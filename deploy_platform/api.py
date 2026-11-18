from __future__ import annotations

import hashlib
import hmac
import secrets
import httpx

import os

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from pathlib import Path
import json
import re
import uuid
from typing import Literal

from . import auth, db, github, cli_auth, config
from .deployer import DeploymentError, deploy, rollback
from . import self_update, discovery, runtimes, restart
from . import deployer

app = FastAPI(title="GitHub Deploy Platform", version="0.1.0")
db.init()
auth.init()
db.recover_interrupted()
INSTANCE = uuid.uuid4().hex

# Reachable without signing in: what the login page needs.
PUBLIC_API = {'/api/auth/state', '/api/auth/login', '/api/auth/setup'}


def _local(request) -> bool:
    """A direct request from this machine, not one passed on by the reverse proxy."""
    return bool(request.client and request.client.host in ('127.0.0.1', '::1', 'testclient')
                and not request.headers.get('x-forwarded-for'))


def _api_token_ok(request) -> bool:
    key = os.environ.get('DEPLOY_ADMIN_TOKEN', '')
    header = request.headers.get('Authorization', '')
    return bool(key) and header.startswith('Bearer ') and hmac.compare_digest(key, header.removeprefix('Bearer '))


@app.middleware("http")
async def admin_access(request, call_next):
    from fastapi.responses import JSONResponse
    path = request.url.path
    if path.startswith('/api/') and path not in PUBLIC_API:
        admin = auth.session_admin(request.cookies.get(auth.COOKIE))
        if admin:
            # A cookie rides along on any request to this site; a custom header cannot be added
            # by another site's form or image, so changes must carry it.
            if request.method not in ('GET', 'HEAD', 'OPTIONS') and request.headers.get('x-deploy-request') != '1':
                return JSONResponse({'detail': '请求缺少 X-Deploy-Request 头'}, status_code=403)
            request.state.admin = admin
        elif _api_token_ok(request):
            request.state.admin = None  # scripts and CI: DEPLOY_ADMIN_TOKEN as a Bearer token
        elif not auth.admin_exists() and not os.environ.get('DEPLOY_ADMIN_TOKEN') and _local(request):
            request.state.admin = None  # nothing configured yet: local development only
        else:
            return JSONResponse({'detail': '请先登录'}, status_code=401)
    return await call_next(request)


class Credentials(BaseModel):
    username: str
    password: str


class SetupRequest(Credentials):
    token: str = ''


class PasswordChange(BaseModel):
    current: str
    new: str


def _set_session(response, request, admin_id):
    response.set_cookie(auth.COOKIE, auth.create_session(admin_id), max_age=auth.SESSION_DAYS * 86400,
                        httponly=True, samesite='strict', secure=request.url.scheme == 'https', path='/')


@app.get('/api/auth/state')
def auth_state(request: Request):
    admin = auth.session_admin(request.cookies.get(auth.COOKIE))
    exists = auth.admin_exists()
    return {'admin_exists': exists, 'logged_in': bool(admin), 'username': admin['username'] if admin else '',
            # The first admin is created with the old management key, or only from this machine when there is none.
            'setup_needs_token': not exists and bool(os.environ.get('DEPLOY_ADMIN_TOKEN')),
            'setup_allowed': not exists and (bool(os.environ.get('DEPLOY_ADMIN_TOKEN')) or _local(request))}


@app.post('/api/auth/setup')
def auth_setup(payload: SetupRequest, request: Request, response: Response):
    if auth.admin_exists():
        raise HTTPException(409, '管理员账户已经存在，请直接登录')
    key = os.environ.get('DEPLOY_ADMIN_TOKEN', '')
    client = request.client.host if request.client else ''
    if key:
        if wait := auth.throttle.wait_seconds(client, 'setup'):
            raise HTTPException(429, f'尝试次数过多，请 {wait // 60 + 1} 分钟后再试')
        if not hmac.compare_digest(key, payload.token):
            auth.throttle.failed(client, 'setup')
            raise HTTPException(401, '管理密钥不正确。它是服务器 .env 里的 DEPLOY_ADMIN_TOKEN')
    elif not _local(request):
        raise HTTPException(403, '服务器没有设置 DEPLOY_ADMIN_TOKEN，只能在服务器本机创建管理员，或运行 python -m deploy_platform admin <用户名>')
    try:
        admin_id = auth.set_admin(payload.username.strip(), payload.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    _set_session(response, request, admin_id)
    return {'ok': True, 'username': payload.username.strip()}


@app.post('/api/auth/login')
def auth_login(payload: Credentials, request: Request, response: Response):
    client = request.client.host if request.client else ''
    username = payload.username.strip()
    if wait := auth.throttle.wait_seconds(client, username):
        raise HTTPException(429, f'尝试次数过多，请 {wait // 60 + 1} 分钟后再试')
    admin = auth.authenticate(username, payload.password)
    if not admin:
        auth.throttle.failed(client, username)
        raise HTTPException(401, '用户名或密码不正确')
    auth.throttle.succeeded(client, username)
    _set_session(response, request, admin['id'])
    return {'ok': True, 'username': admin['username']}


@app.post('/api/auth/logout')
def auth_logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, path='/')
    return {'ok': True}


@app.post('/api/auth/password')
def auth_password(payload: PasswordChange, request: Request):
    admin = getattr(request.state, 'admin', None)
    if not admin:
        raise HTTPException(400, '请用管理员账户登录后再修改密码')
    try:
        auth.change_password(admin['id'], payload.current, payload.new, keep_token=request.cookies.get(auth.COOKIE))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {'ok': True}



def _check_branch(value):
    if not value or value.startswith('-') or any(c.isspace() for c in value) or '..' in value or any(c in value for c in '~^:?*[\\'):
        raise ValueError('分支名称无效')
    return value


class ProjectCreate(BaseModel):
    account_id: int
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = "main"
    deploy_root: str
    build_command: str = ""
    start_command: str = ""
    healthcheck_url: str = ""
    healthcheck_timeout: int = Field(default=45, ge=5, le=600)
    webhook_secret: str = ""
    runtime: Literal['command','compose','systemd','container'] = 'command'
    runtime_config: dict = Field(default_factory=dict)

    @field_validator('runtime_config', mode='before')
    @classmethod
    def parse_runtime_config(cls, value):
        return json.loads(value) if isinstance(value, str) else value

    @field_validator('branch')
    @classmethod
    def valid_branch(cls, value):
        return _check_branch(value)

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


def _connect_cli_account():
    """Store the server's gh identity as an account. The token itself stays with gh."""
    user = cli_auth.identity()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO github_accounts(github_id,login,access_token,created_at,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(github_id) DO UPDATE SET login=excluded.login,access_token=excluded.access_token,updated_at=excluded.updated_at",
            (user['github_id'], user['login'], 'gh-cli:'+user['github_id'], db.now(), db.now()))
    return user


@app.get("/api/accounts")
def accounts():
    with db.connect() as conn:
        empty = not conn.execute("SELECT 1 FROM github_accounts").fetchone()
    if empty:
        # A logged-in gh is all that is needed; do not make people find a "connect" button first.
        try:
            _connect_cli_account()
        except RuntimeError:
            pass  # not logged in; the GitHub page shows gh's own message
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
    runtime: Literal['command','compose','systemd','container'] = 'command'
    runtime_config: dict = Field(default_factory=dict)
    start_command: str = ''
    build_command: str = ''
    healthcheck_url: str = ''
    healthcheck_timeout: int = Field(default=45, ge=5, le=600)
    # Only for directories that are not a Git checkout: which GitHub repository the app comes from.
    repository: str = ''
    branch: str = ''


def _mark_imported(result):
    with db.connect() as c:
        roots = {str(Path(r[0]).resolve()) for r in c.execute('SELECT deploy_root FROM projects')}
    for item in result['projects']:
        item['imported'] = item['path'] in roots
    return result


@app.post('/api/discovery/scan')
def scan_local(payload: DiscoveryRequest):
    try:
        return _mark_imported(discovery.scan(payload.root, payload.depth))
    except OSError:
        raise HTTPException(400, '目录不存在或无法读取') from None


@app.get('/api/discovery/running')
def scan_running():
    """Projects found from running Compose projects and systemd services; no directory needed."""
    return _mark_imported(discovery.running())


@app.post('/api/projects/import')
def import_project(payload: ImportRequest):
    try:
        item = discovery.describe(payload.path)
    except OSError:
        raise HTTPException(400, '目录不存在或无法读取') from None
    root = Path(item['path'])
    if root == Path(root.anchor) or not discovery._app_path(str(root)):
        raise HTTPException(400, '请选择应用自己的目录，不要选系统目录')
    if item['kind'] == 'git':
        if not item['repository'] or not item['branch']:
            raise HTTPException(400, item['note'] or '需要 GitHub origin 和已检出的本地分支')
        mode, repository, branch, message = 'existing', item['repository'], item['branch'], '已登记当前提交；未修改代码或重启服务'
    else:
        # Not a Git checkout: the platform never touches the files, it runs build and restart.
        repository = (payload.repository or item['repository']).strip()
        branch = (payload.branch or item['branch'] or 'main').strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
            raise HTTPException(400, '请选择这个应用对应的 GitHub 仓库')
        try:
            _check_branch(branch)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        mode, message = 'external', '已登记；这个目录不是 Git 仓库，平台不会改动里面的文件'
    settings = runtimes.validate(payload.runtime, payload.runtime_config, item['path'])
    if payload.runtime == 'command' and not payload.start_command.strip():
        raise HTTPException(400, '请填写现有服务的重启命令')
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM github_accounts WHERE id=?',(payload.account_id,)).fetchone():
            raise HTTPException(404,'GitHub 账号不存在')
        paths=[Path(r[0]).resolve() for r in c.execute('SELECT deploy_root FROM projects')]
        if any(root==p or root.is_relative_to(p) or p.is_relative_to(root) for p in paths):
            raise HTTPException(409,'目录与已有项目重叠')
        now=db.now()
        job=c.execute("INSERT INTO projects(account_id,repository,branch,deploy_root,build_command,start_command,healthcheck_url,webhook_secret,created_at,updated_at,mode,runtime,runtime_config,healthcheck_timeout) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload.account_id,repository,branch,item['path'],payload.build_command,payload.start_command,payload.healthcheck_url,secrets.token_hex(32),now,now,mode,payload.runtime,json.dumps(settings),payload.healthcheck_timeout)).lastrowid
        c.execute("INSERT INTO deployments(project_id,action,status,commit_sha,release_dir,message,started_at,finished_at) VALUES (?,'import','succeeded',?,?,?,?,?)",(job,item['commit'],item['path'],message+'\n',now,now))
    return {'id':job}


@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    # Compose restarts the service itself after each deployment; a command must do so on its own.
    settings = runtimes.validate_release(payload.runtime, payload.runtime_config, payload.repository)
    if payload.runtime == 'command' and not payload.start_command.strip():
        raise HTTPException(400, "请填写启动命令，或把运行方式改为 Docker Compose")
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
            "INSERT INTO projects(account_id, repository, branch, deploy_root, build_command, start_command, healthcheck_url, webhook_secret, created_at, updated_at, healthcheck_timeout, runtime, runtime_config) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payload.account_id, payload.repository, payload.branch, payload.deploy_root,
             payload.build_command, payload.start_command, payload.healthcheck_url,
             webhook_secret,
             timestamp, timestamp, payload.healthcheck_timeout, payload.runtime, json.dumps(settings)),
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


@app.post("/api/projects/{project_id}/restart")
def restart_project(project_id: int):
    """Restart the running version only: no fetch, no build."""
    project = project_detail(project_id)
    return {"deployment_id": deploy(project_id, account={}, project=project, action="restart"), "status": "queued"}


@app.get("/api/projects/status")
def projects_status():
    """Service state of every project, for the project list."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM projects")]
    return {str(p['id']): {k: v for k, v in runtimes.status(p).items() if k in ('state', 'detail')} for p in rows}


@app.get("/api/projects/{project_id}/status")
def project_status(project_id: int):
    return runtimes.status(project_detail(project_id))


@app.get("/api/projects/{project_id}/logs")
def project_logs(project_id: int, lines: int = Query(200, ge=20, le=2000)):
    try:
        return runtimes.logs(project_detail(project_id), lines)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception:
        raise HTTPException(502, '读取服务日志失败：Docker 或 journalctl 不可用') from None


@app.get("/api/projects/{project_id}/deployments")
def deployments(project_id: int, limit: int = Query(50, ge=1, le=200)):
    with db.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM deployments WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))]


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
    deployment_id = deploy(project_id, account=dict(row), project=dict(row), action="webhook", queue=True)
    if deployment_id is None:
        # A job is running; it starts one more update when it ends, so this push is not lost.
        return {"ok": True, "queued": True}
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
    return _connect_cli_account()


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
    runtime = payload.runtime
    if project['mode'] in ('existing', 'external'):
        settings = runtimes.validate(runtime, payload.runtime_config, project['deploy_root'])
    else:
        settings = runtimes.validate_release(runtime, payload.runtime_config, project['repository'])
    if runtime == 'command' and not payload.start_command.strip():
        raise HTTPException(400, "请填写重启命令")
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM deployments WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone():
            raise HTTPException(409, "部署进行中，暂时不能修改配置")
        conn.execute("UPDATE projects SET branch=?,build_command=?,start_command=?,healthcheck_url=?,healthcheck_timeout=?,runtime=?,runtime_config=?,updated_at=? WHERE id=?",
                     (payload.branch,payload.build_command,payload.start_command,payload.healthcheck_url,payload.healthcheck_timeout,runtime,json.dumps(settings),db.now(),project_id))
    return {"ok": True}


WEB = Path(__file__).with_name("web")


def _asset_version(name: str) -> str:
    return hashlib.sha256((WEB / name).read_bytes()).hexdigest()[:12]


# Script and style URLs carry a hash of their content: a new version is a new URL, so neither the
# browser nor Cloudflare (which adds a 4 h browser cache to static files) can keep serving an old one.
# Files only change by a self update, which restarts the process, so hashing once at start is enough.
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
for _asset in ("style.css", "discovery.js", "app.js"):
    INDEX_HTML = INDEX_HTML.replace(f'"/{_asset}"', f'"/{_asset}?v={_asset_version(_asset)}"')


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def static_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if not path.startswith(("/api/", "/webhooks/", "/health")) and "cache-control" not in response.headers:
        # Versioned URLs never change; anything else is checked with the server every time.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.query_params.get("v") else "no-cache"
    return response


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

