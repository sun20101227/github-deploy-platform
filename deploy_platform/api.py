from __future__ import annotations

import hashlib
import hmac
import secrets
import httpx

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pathlib import Path
import sqlite3
import json
import uuid
from typing import Literal

from . import db, github, cli_auth, config
from .deployer import DeploymentError, deploy, rollback
from . import self_update, discovery, runtimes, restart
from . import deployer

app = FastAPI(title="GitHub Deploy Platform", version="0.1.0")
db.init()
oauth_states: set[str] = set()
INSTANCE = uuid.uuid4().hex


@app.middleware("http")
async def admin_access(request, call_next):
    import os
    from fastapi.responses import JSONResponse
    if request.url.path.startswith('/api/'):
        key = os.environ.get('DEPLOY_ADMIN_TOKEN', '')
        provided = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if key:
            allowed = hmac.compare_digest(key, provided)
        else:
            allowed = request.client and request.client.host in ('127.0.0.1', '::1', 'testclient') and not request.headers.get('x-forwarded-for')
        if not allowed:
            return JSONResponse({'detail': '请输入管理密钥；服务器需设置 DEPLOY_ADMIN_TOKEN'}, status_code=401)
    return await call_next(request)



class ProjectCreate(BaseModel):
    account_id: int
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = "main"
    deploy_root: str
    build_command: str = ""
    start_command: str
    healthcheck_url: str = ""
    webhook_secret: str = ""
    runtime: Literal['command','compose','systemd'] = 'command'
    runtime_config: dict = Field(default_factory=dict)

    @field_validator('runtime_config', mode='before')
    @classmethod
    def parse_runtime_config(cls, value):
        return json.loads(value) if isinstance(value, str) else value

    @field_validator('branch')
    @classmethod
    def valid_branch(cls, value):
        if not value or value.startswith('-') or any(c.isspace() for c in value) or '..' in value or any(c in value for c in '~^:?*[\\'):
            raise ValueError('分支名称无效')
        return value

    @field_validator('deploy_root')
    @classmethod
    def valid_root(cls, value):
        target = Path(value).resolve()
        if not Path(value).is_absolute():
            raise ValueError('请输入服务器上的绝对路径')
        if target == Path(target.anchor) or target == config.ROOT or target in config.ROOT.parents:
            raise ValueError('请选择独立的项目目录')
        return str(target)

@app.get("/health")
def health():
    return {"ok": True, "instance": INSTANCE}


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
    return RedirectResponse("/#/github", status_code=303)


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
    return await github.repositories(cli_auth.token(row["access_token"]))


@app.get("/api/projects")
def projects():
    with db.connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT p.*, a.login AS github_login FROM projects p JOIN github_accounts a ON a.id=p.account_id ORDER BY p.id DESC"
        )]


@app.get("/api/self-update")
def self_update_status():
    return {**self_update.status(), "automatic_restart": restart.supported(), "instance": INSTANCE}


@app.post("/api/self-update/check")
def self_update_check():
    try:
        return self_update.check()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/self-update/apply")
def self_update_apply():
    if not restart.supported():
        raise HTTPException(409, "请先使用 python -m deploy_platform 启动，更新后才能自动重启")
    with deployer._guard:
        if deployer.maintenance.is_set() or any(lock.locked() for lock in deployer._locks.values()):
            raise HTTPException(409, '请等当前更新任务结束')
        deployer.maintenance.set()
    try:
        result = self_update.apply()
        restart.schedule()
        return {**result, "restarting": True, "instance": INSTANCE}
    except Exception as exc:
        deployer.maintenance.clear()
        raise HTTPException(409, str(exc)) from exc



class DiscoveryRequest(BaseModel):
    root: str
    depth: int = Field(default=2, ge=0, le=4)


class ImportRequest(BaseModel):
    path: str
    account_id: int
    runtime: Literal['command','compose','systemd'] = 'command'
    runtime_config: dict = Field(default_factory=dict)
    start_command: str = ''
    build_command: str = ''
    healthcheck_url: str = ''


@app.post('/api/discovery/scan')
def scan_local(payload: DiscoveryRequest):
    try:
        return discovery.scan(payload.root, payload.depth)
    except OSError:
        raise HTTPException(400, '目录不存在或无法读取') from None


@app.post('/api/projects/import')
def import_project(payload: ImportRequest):
    try:
        item = discovery.inspect(payload.path)
    except OSError:
        raise HTTPException(400, '目录不存在或无法读取') from None
    if not item['repository'] or not item['branch']:
        raise HTTPException(400, '需要 GitHub origin 和已检出的本地分支')
    settings = runtimes.validate(payload.runtime, payload.runtime_config, item['path'])
    if payload.runtime == 'command' and not payload.start_command.strip():
        raise HTTPException(400, '请填写现有服务的重启命令')
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM github_accounts WHERE id=?',(payload.account_id,)).fetchone():
            raise HTTPException(404,'GitHub 账号不存在')
        paths=[Path(r[0]).resolve() for r in c.execute('SELECT deploy_root FROM projects')]
        root=Path(item['path'])
        if any(root==p or root.is_relative_to(p) or p.is_relative_to(root) for p in paths):
            raise HTTPException(409,'目录与已有项目重叠')
        now=db.now()
        job=c.execute("INSERT INTO projects(account_id,repository,branch,deploy_root,build_command,start_command,healthcheck_url,webhook_secret,created_at,updated_at,mode,runtime,runtime_config) VALUES (?,?,?,?,?,?,?,?,?,?,'existing',?,?)",
            (payload.account_id,item['repository'],item['branch'],item['path'],payload.build_command,payload.start_command,payload.healthcheck_url,secrets.token_hex(32),now,now,payload.runtime,json.dumps(settings))).lastrowid
        c.execute("INSERT INTO deployments(project_id,action,status,commit_sha,release_dir,message,started_at,finished_at) VALUES (?,'import','succeeded',?,?,'已登记当前提交；未修改代码或重启服务',?,?)",(job,item['commit'],item['path'],now,now))
    return {'id':job}


@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    if payload.runtime != 'command':
        raise HTTPException(400, '新建项目请填写包含重启步骤的启动命令')
    if not payload.start_command.strip():
        raise HTTPException(400, "请填写启动命令")
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM github_accounts WHERE id=?", (payload.account_id,)).fetchone():
            raise HTTPException(404, "GitHub 账号不存在")
        root = Path(payload.deploy_root)
        paths = [Path(r[0]).resolve() for r in conn.execute('SELECT deploy_root FROM projects')]
        if any(root == p or root.is_relative_to(p) or p.is_relative_to(root) for p in paths):
            raise HTTPException(409, "这个部署目录与已有项目重叠")
        if (root / '.git').exists():
            raise HTTPException(409, "目录已有 Git 仓库，请从已有项目页面接入")
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
    deployment_id = deploy(project_id, account=dict(account), project=dict(row))
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
    latest = await github.latest_commit(cli_auth.token(row["access_token"]), row["repository"], row["branch"])
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
        return {"deployment_id": rollback(project_id, dict(row)), "status": "queued"}
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
    if request.headers.get('x-github-event') != 'push' or payload.get('deleted') or payload.get('repository', {}).get('full_name') != row['repository']:
        return {"ok": True, "ignored": True}
    ref = payload.get("ref", "")
    if ref != "refs/heads/" + row["branch"]:
        return {"ok": True, "ignored": True, "reason": "branch"}
    deployment_id = deploy(project_id, account=dict(row), project=dict(row), action="webhook")
    return {"ok": True, "deployment_id": deployment_id}


@app.exception_handler(RuntimeError)
async def runtime_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(httpx.HTTPError)
async def upstream_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "GitHub 请求失败，请检查网络和账号权限"}, status_code=502)


@app.get("/api/github/status")
def github_status():
    try:
        return {"connected": True, **cli_auth.identity()}
    except RuntimeError as exc:
        return {"connected": False, "message": str(exc)}


@app.post("/api/github/connect")
def github_connect():
    user = cli_auth.identity()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO github_accounts(github_id,login,access_token,created_at,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(github_id) DO UPDATE SET login=excluded.login,access_token=excluded.access_token,updated_at=excluded.updated_at",
            (user['github_id'], user['login'], 'gh-cli:'+user['github_id'], db.now(), db.now()))
    return user


@app.get("/api/projects/{project_id}")
def project_detail(project_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT p.*, a.login AS github_login FROM projects p JOIN github_accounts a ON a.id=p.account_id WHERE p.id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(404, "项目不存在")
    return dict(row)


@app.put("/api/projects/{project_id}")
def edit_project(project_id: int, payload: ProjectCreate):
    project = project_detail(project_id)
    runtime = payload.runtime if project['mode'] == 'existing' else 'command'
    settings = runtimes.validate(runtime, payload.runtime_config, project['deploy_root'])
    if runtime == 'command' and not payload.start_command.strip():
        raise HTTPException(400, "请填写重启命令")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM deployments WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
            raise HTTPException(409, "部署进行中，暂时不能修改配置")
        conn.execute("UPDATE projects SET branch=?,build_command=?,start_command=?,healthcheck_url=?,runtime=?,runtime_config=?,updated_at=? WHERE id=?",
                     (payload.branch,payload.build_command,payload.start_command,payload.healthcheck_url,runtime,json.dumps(settings),db.now(),project_id))
    return {"ok": True}


app.mount("/", StaticFiles(directory=__import__("pathlib").Path(__file__).with_name("web"), html=True), name="web")

