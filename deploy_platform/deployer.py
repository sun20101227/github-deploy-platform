"""Prepare releases separately; serialize activation and restore the last service on failure."""
import base64
import functools
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
import httpx
from tenacity import RetryError, Retrying, retry_if_result, stop_after_delay, wait_fixed
from . import config, db, cli_auth, existing, runtimes

class DeploymentError(RuntimeError):
    pass

_locks = {}
_pending = set()  # projects that received a push while a job was running
_guard = threading.Lock()
maintenance = threading.Event()


def _writable(func, path, _):
    # Git marks object files read-only; Windows refuses to delete those.
    os.chmod(path, stat.S_IWRITE)
    func(path)

_RMTREE = {'onexc': _writable} if sys.version_info >= (3, 12) else {'onerror': _writable}


def _prune(root, keep):
    """Delete old numbered release directories, never the ones listed in keep."""
    releases = (root / 'releases').resolve()
    protected = {Path(p).resolve() for p in keep if p}
    numbered = sorted((p for p in releases.iterdir() if p.name.isdigit() and p.is_dir() and not p.is_symlink()),
                      key=lambda p: int(p.name), reverse=True)
    removed = 0
    for path in numbered[config.KEEP_RELEASES:]:
        if path.resolve() not in protected:
            shutil.rmtree(path, **_RMTREE)
            removed += 1
    return removed

LOG_LIMIT = 60000  # characters of output kept per deployment record


def _append(job, text):
    with db.connect() as c:
        c.execute("UPDATE deployments SET message=substr(message || ?, -?) WHERE id=?", (text, LOG_LIMIT, job))


def _kill(proc):
    try:
        if os.name == 'nt':
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)  # the shell and everything it started
    except (OSError, ProcessLookupError):
        pass


def _run(args, *, cwd=None, env=None, timeout=900, job=None):
    """Run a command. With job, its output goes into that deployment record line by line while it
    runs (the console polls it), and nothing is returned; without, the output is returned."""
    if job is None:
        r = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode:
            raise DeploymentError((r.stderr or r.stdout or '命令执行失败')[-1600:])
        return r.stdout.strip()
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors='replace', start_new_session=os.name != 'nt')
    lines, guard = [], threading.Lock()

    def read():
        for line in proc.stdout:
            with guard:
                lines.append(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def flush(final=False):
        with guard:
            chunk = ''.join(lines)
            lines.clear()
        if final and chunk and not chunk.endswith('\n'):
            chunk += '\n'
        if chunk:
            _append(job, chunk)

    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if time.monotonic() > deadline:
            _kill(proc)
            proc.wait()
            reader.join(2)
            flush(final=True)
            raise DeploymentError(f'命令超过 {timeout // 60} 分钟没有结束，已停止')
        time.sleep(0.5)
        flush()
    reader.join(5)
    flush(final=True)
    if proc.returncode:
        raise DeploymentError(f'命令失败（退出码 {proc.returncode}），输出见上方')
    return ''

def _current_release(root):
    p = root / 'current'
    return p.read_text(encoding='utf-8').strip() if p.exists() else ''

def _pointer(root, target):
    if not target:
        (root / 'current').unlink(missing_ok=True)
        return
    tmp = root / ('.current-' + uuid.uuid4().hex)
    tmp.write_text(str(target), encoding='utf-8')
    os.replace(tmp, root / 'current')

def _healthy(url):
    try:
        return 200 <= httpx.get(url, timeout=2, follow_redirects=False).status_code < 300
    except httpx.HTTPError:
        return False

def _start(project, target, job=None, pull=False):
    env = os.environ.copy()
    env['DEPLOY_RELEASE_DIR'] = str(target)
    env['DEPLOY_PROJECT_ROOT'] = project['deploy_root']
    if not runtimes.restart(project, target, pull=pull):
        command = project['start_command']
        # An app's own updater (e.g. systemctl start qiyu-updater) can build for several minutes.
        _run(['cmd', '/c', command] if os.name == 'nt' else ['sh', '-c', command], cwd=target, env=env, timeout=1800, job=job)
    url = project.get('healthcheck_url')
    if url:
        wait = int(project.get('healthcheck_timeout') or 45)
        try:
            Retrying(stop=stop_after_delay(wait), wait=wait_fixed(1), sleep=time.sleep,
                     retry=retry_if_result(lambda healthy: not healthy))(_healthy, url)
        except RetryError:
            raise DeploymentError(f'服务健康检查未通过（已等待 {wait} 秒）') from None

def _note(job, text):
    with db.connect() as c:
        c.execute("UPDATE deployments SET message=message || ? WHERE id=?", (text+'\n',job))

def deploy(project_id, *, account, project, action='deploy', queue=False):
    """Start a job. With queue=True a busy project returns None and runs once more afterwards."""
    with _guard:
        if maintenance.is_set():
            raise DeploymentError('平台正在更新，请等待重启完成')
        lock = _locks.setdefault(project_id, threading.Lock())
        if not lock.acquire(False):
            if queue:
                _pending.add(project_id)
                return None
            raise DeploymentError('已有部署正在进行')
    try:
        with db.connect() as c:
            job = c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,?,'queued',?)",(project_id,action,db.now())).lastrowid
        threading.Thread(target=_worker,args=(job,account,project,action,lock),daemon=True).start()
        return job
    except Exception:
        lock.release()
        raise

def rollback(project_id, project):
    column = 'previous_commit' if project.get('mode') == 'existing' else 'previous_release'
    with db.connect() as c:
        previous = c.execute(f"SELECT {column} FROM deployments WHERE project_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",(project_id,)).fetchone()
    if not previous or not previous[column]:
        raise DeploymentError('没有可回滚的版本')
    return deploy(project_id,account={},project=project,action='rollback')

def _worker(job, account, project, action, lock):
    try:
        _execute(job, account, project, action)
    finally:
        _finish(project.get('id'), lock)

def _finish(project_id, lock):
    # Release and read the pending flag in one step, so a push arriving now is either
    # queued before this point or finds the lock free and starts its own job.
    with _guard:
        lock.release()
        again = project_id in _pending
        _pending.discard(project_id)
    if again:
        try:
            _rerun(project_id)
        except Exception:
            pass  # e.g. the platform entered maintenance; the next push or manual update catches up

def _rerun(project_id):
    with db.connect() as c:
        project = c.execute('SELECT * FROM projects WHERE id=?',(project_id,)).fetchone()
        account = project and c.execute('SELECT * FROM github_accounts WHERE id=?',(project['account_id'],)).fetchone()
    if project and account:
        deploy(project_id,account=dict(account),project=dict(project),action='webhook',queue=True)

def _external(job, project, action):
    """An app that is not a Git checkout (copied files, an image, or its own updater):
    leave the files alone, run the build and the restart, check health."""
    root = Path(project['deploy_root'])
    try:
        if action == 'rollback':
            raise DeploymentError('这个项目的版本由它自己管理，平台不能回滚')
        with db.connect() as c:
            c.execute("UPDATE deployments SET status='running',release_dir=? WHERE id=?",(str(root),job))
        if project['build_command']:
            _note(job,'执行构建')
            cmd = project['build_command']
            _run(['cmd','/c',cmd] if os.name=='nt' else ['sh','-c',cmd],cwd=root,timeout=1800,job=job)
        image_app = project.get('runtime') == 'compose'
        _note(job,'拉取最新镜像，重启服务并检查状态' if image_app else '重启服务并检查状态')
        # No code to fetch here: for a Compose app the newer image is the update.
        _start(project,root,job,pull=image_app)
        _note(job,'更新与重启完成')
        status = 'succeeded'
    except Exception as exc:
        _note(job,str(exc) if isinstance(exc,RuntimeError) else '更新或重启失败，请检查服务日志')
        status = 'failed'
    with db.connect() as c:
        c.execute('UPDATE deployments SET status=?,finished_at=? WHERE id=?',(status,db.now(),job))

def _restart_only(job, project):
    """Restart the running version without touching code. Keeps the version fields of the last
    successful deployment, so "check for updates" and rollback still see the same version."""
    root = Path(project['deploy_root']).resolve()
    status = 'failed'
    try:
        with db.connect() as c:
            last = c.execute("SELECT commit_sha,release_dir,previous_release,previous_commit FROM deployments "
                             "WHERE project_id=? AND status='succeeded' AND action<>'restart' ORDER BY id DESC LIMIT 1",(project['id'],)).fetchone()
            last = dict(last) if last else {'commit_sha':'','release_dir':'','previous_release':'','previous_commit':''}
            c.execute("UPDATE deployments SET status='running',commit_sha=?,release_dir=?,previous_release=?,previous_commit=? WHERE id=?",
                      (last['commit_sha'],last['release_dir'],last['previous_release'],last['previous_commit'],job))
        if project.get('mode') in ('existing','external'):
            target = root
        else:
            current = _current_release(root)
            if not current:
                raise DeploymentError('还没有部署成功过，没有可以重启的版本')
            target = Path(current)
        _note(job,'重启服务并检查状态')
        _start(project,target,job)
        _note(job,'服务已重启' + ('，健康检查通过' if project.get('healthcheck_url') else ''))
        status = 'succeeded'
    except Exception as exc:
        _note(job,str(exc) if isinstance(exc,RuntimeError) else '重启失败，请查看服务日志')
    with db.connect() as c:
        c.execute('UPDATE deployments SET status=?,finished_at=? WHERE id=?',(status,db.now(),job))

def _execute(job, account, project, action):
    if action == 'restart':
        _restart_only(job, project)
        return
    if project.get('mode') == 'external':
        _external(job, project, action)
        return
    if project.get('mode') == 'existing':
        try:
            existing.execute(job,account,project,action,_note,functools.partial(_start,job=job),functools.partial(_run,job=job))
        except Exception:
            _note(job,'无法读取已有项目目录')
            with db.connect() as c:
                c.execute("UPDATE deployments SET status='failed',finished_at=? WHERE id=?",(db.now(),job))
        return
    root = Path(project['deploy_root']).resolve()
    previous = ''
    activated = False
    secret = ''
    try:
        root.mkdir(parents=True,exist_ok=True)
        previous = _current_release(root)
        with db.connect() as c:
            c.execute("UPDATE deployments SET status='running',previous_release=? WHERE id=?",(previous,job))
        if action == 'rollback':
            with db.connect() as c:
                row=c.execute("SELECT previous_release FROM deployments WHERE project_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",(project['id'],)).fetchone()
            target = Path(row['previous_release']).resolve()
            if target.parent != (root/'releases').resolve() or not target.is_dir():
                raise DeploymentError('回滚目录不存在或不属于这个项目')
            _note(job,'准备回滚版本')
        else:
            target=root/'releases'/str(job)
            target.parent.mkdir(parents=True,exist_ok=True)
            secret=cli_auth.token(account['access_token'])
            env=os.environ.copy()
            env.update(GIT_TERMINAL_PROMPT='0',GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                       GIT_CONFIG_VALUE_0='Authorization: Basic '+base64.b64encode(('x-access-token:'+secret).encode()).decode())
            _note(job,'拉取代码')
            _run(['git','clone','--depth','1','--branch',project['branch'],'--','https://github.com/'+project['repository']+'.git',str(target)],env=env)
            if project['build_command']:
                _note(job,'执行构建')
                cmd=project['build_command']
                _run(['cmd','/c',cmd] if os.name=='nt' else ['sh','-c',cmd],cwd=target,timeout=1800,job=job)
        sha=_run(['git','rev-parse','HEAD'],cwd=target)
        with db.connect() as c:
            c.execute('UPDATE deployments SET release_dir=?,commit_sha=? WHERE id=?',(str(target),sha,job))
        _note(job,'切换版本，启动服务')
        _pointer(root,target)
        activated=True
        _start(project,target,job)
        _note(job,'健康检查通过' if project.get('healthcheck_url') else '服务已重启（未配置健康检查）')
        with db.connect() as c:
            c.execute("UPDATE deployments SET status='succeeded',finished_at=? WHERE id=?",(db.now(),job))
        try:
            # previous stays: it is the target of the next manual rollback.
            removed=_prune(root,[target,previous])
            if removed:
                _note(job,f'已清理 {removed} 个旧版本目录')
        except Exception:
            _note(job,'旧版本目录清理未完成，可稍后手动删除')
    except Exception as exc:
        status='failed'
        _note(job,str(exc).replace(secret,'[redacted]') if secret else str(exc))
        if activated:
            try:
                _pointer(root,previous)
                if previous:
                    _start(project,Path(previous),job)
                    status='rolled_back'
                    _note(job,'已恢复上一版本并重新启动')
                else:
                    _note(job,'首次部署失败，已清除版本指针；请检查进程管理器')
            except Exception:
                status='rollback_failed'
                _note(job,'旧版本启动失败，请检查服务日志')
        with db.connect() as c:
            c.execute('UPDATE deployments SET status=?,finished_at=? WHERE id=?',(status,db.now(),job))
