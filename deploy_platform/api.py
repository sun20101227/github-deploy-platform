from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, github
from .deployer import DeploymentError, deploy, rollback

app = FastAPI(title="GitHub Deploy Platform", version="0.1.0")
db.init()
oauth_states: set[str] = set()


class ProjectCreate(BaseModel):
    account_id: int
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = "main"
    deploy_root: str
    build_command: str = ""
    start_command: str
    healthcheck_url: str = ""
    webhook_secret: str = ""




@app.get("/health")
def health():
    return {"ok": True}


@app.get("/auth/github/start")
def github_start():
    try:
        url, state = github.authorization_url()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    oauth_states.add(state)
    return RedirectResponse(url)


@app.get("/auth/github/callback")
async def github_callback(code: str = Query(...), state: str = Query(...)):
    if state not in oauth_states:
        raise HTTPException(400, "OAuth state 无效或已使用")
    oauth_states.remove(state)
    account = await github.exchange_code(code)
    with db.connect() as conn:
        timestamp = db.now()
        conn.execute(
            "INSERT INTO github_accounts(github_id, login, access_token, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(github_id) DO UPDATE SET login=excluded.login, access_token=excluded.access_token, updated_at=excluded.updated_at",
            (account["github_id"], account["login"], account["access_token"], timestamp, timestamp),
        )
        row = conn.execute("SELECT id, login FROM github_accounts WHERE github_id=?", (account["github_id"],)).fetchone()
    return {"account_id": row["id"], "login": row["login"]}


@app.get("/api/accounts")
def accounts():
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT id, github_id, login, created_at, updated_at FROM github_accounts")]


@app.get("/api/accounts/{account_id}/repositories")
async def account_repositories(account_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT access_token FROM github_accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise HTTPException(404, "GitHub 账号不存在")
    return await github.repositories(row["access_token"])


@app.get("/api/projects")
def projects():
    with db.connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT p.*, a.login AS github_login FROM projects p JOIN github_accounts a ON a.id=p.account_id ORDER BY p.id DESC"
        )]


@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM github_accounts WHERE id=?", (payload.account_id,)).fetchone():
            raise HTTPException(404, "GitHub 账号不存在")
        timestamp = db.now()
        webhook_secret = payload.webhook_secret or secrets.token_hex(32)
        cursor = conn.execute(
            "INSERT INTO projects(account_id, repository, branch, deploy_root, build_command, start_command, healthcheck_url, webhook_secret, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payload.account_id, payload.repository, payload.branch, payload.deploy_root,
             payload.build_command, payload.start_command, payload.healthcheck_url,
             webhook_secret,
             timestamp, timestamp),
        )
        return {"id": cursor.lastrowid, "webhook_secret": webhook_secret}


@app.post("/api/projects/{project_id}/deploy")
def deploy_project(project_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(404, "项目不存在")
        account = conn.execute("SELECT * FROM github_accounts WHERE id=?", (row["account_id"],)).fetchone()
    deployment_id = deploy(project_id, dict(account), dict(row))
    return {"deployment_id": deployment_id, "status": "queued"}


@app.post("/api/projects/{project_id}/update")
def update_project(project_id: int):
    """更新就是按项目当前分支重新准备一个候选版本。"""
    return deploy_project(project_id)


@app.get("/api/projects/{project_id}/check")
async def check_project(project_id: int):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT p.*, a.access_token FROM projects p JOIN github_accounts a ON a.id=p.account_id WHERE p.id=?",
            (project_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    latest = await github.latest_commit(row["access_token"], row["repository"], row["branch"])
    with db.connect() as conn:
        current = conn.execute(
            "SELECT commit_sha FROM deployments WHERE project_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    return {**latest, "current_sha": current["commit_sha"] if current else "", "available": not current or current["commit_sha"] != latest["sha"]}


@app.post("/api/projects/{project_id}/rollback")
def rollback_project(project_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    try:
        return {"deployment_id": rollback(project_id, dict(row)), "status": "succeeded"}
    except DeploymentError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/projects/{project_id}/deployments")
def deployments(project_id: int):
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM deployments WHERE project_id=? ORDER BY id DESC", (project_id,))]


@app.post("/webhooks/github/{project_id}")
async def github_webhook(project_id: int, request: Request):
    """Trigger an update only for a signed push on the configured branch."""
    body = await request.body()
    signature = request.headers.get("x-hub-signature-256", "")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT p.*, a.access_token FROM projects p JOIN github_accounts a ON a.id=p.account_id WHERE p.id=?",
            (project_id,),
        ).fetchone()
    if not row or not row["webhook_secret"]:
        raise HTTPException(404, "Webhook 不存在")
    expected = "sha256=" + hmac.new(row["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Webhook 签名无效")
    payload = await request.json()
    ref = payload.get("ref", "")
    if ref != "refs/heads/" + row["branch"]:
        return {"ok": True, "ignored": True, "reason": "branch"}
    deployment_id = deploy(project_id, dict(row), dict(row), action="webhook")
    return {"ok": True, "deployment_id": deployment_id}


app.mount("/", StaticFiles(directory=__import__("pathlib").Path(__file__).with_name("web"), html=True), name="web")

