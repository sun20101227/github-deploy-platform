from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_state = {"phase": "idle", "message": ""}


def _root() -> Path:
    return config.ROOT


def _run(args: list[str], *, env=None, timeout=60) -> str:
    result = subprocess.run(args, cwd=_root(), env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()
        raise RuntimeError(message[-1000:] or f"命令失败：{' '.join(args)}")
    return result.stdout.strip()


def _auth_env() -> dict:
    if not shutil.which("gh"):
        raise RuntimeError("服务器没有 gh，请先安装 GitHub CLI 并运行 gh auth login")
    token = _run(["gh", "auth", "token"], timeout=20)
    if not token:
        raise RuntimeError("gh 尚未登录，请先运行 gh auth login")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: bearer {token}"
    return env


def _info() -> dict:
    branch = _run(["git", "branch", "--show-current"])
    remote = _run(["git", "remote", "get-url", "origin"])
    commit = _run(["git", "rev-parse", "HEAD"])
    return {"branch": branch or "main", "remote": remote, "commit": commit}


def status() -> dict:
    try:
        return {**_state, **_info(), "gh_available": bool(shutil.which("gh"))}
    except Exception as exc:
        return {**_state, "error": str(exc), "gh_available": bool(shutil.which("gh"))}


def check() -> dict:
    env = _auth_env()
    info = _info()
    _run(["git", "fetch", "--quiet", "origin", info["branch"]], env=env, timeout=180)
    remote_commit = _run(["git", "rev-parse", f"origin/{info['branch']}"])
    behind = remote_commit != info["commit"]
    return {**info, "latest_commit": remote_commit, "available": behind}


def apply() -> dict:
    if not _lock.acquire(blocking=False):
        raise RuntimeError("本体更新已经在进行")
    try:
        _state.update(phase="updating", message="正在通过 gh 登录凭证拉取本体")
        env = _auth_env()
        info = _info()
        dirty = _run(["git", "status", "--porcelain"])
        if dirty:
            raise RuntimeError("当前工作区有未提交修改，请先提交或清理后再更新")
        _run(["git", "fetch", "origin", info["branch"]], env=env, timeout=300)
        _run(["git", "merge-base", "--is-ancestor", info["commit"], f"origin/{info['branch']}"], timeout=30)
        _run(["git", "pull", "--ff-only", "origin", info["branch"]], env=env, timeout=300)
        new_commit = _run(["git", "rev-parse", "HEAD"])
        _state.update(phase="restarting", message="本体更新完成，等待进程管理器重启", commit=new_commit)
        return {"ok": True, "restart_required": True, "commit": new_commit}
    finally:
        _lock.release()

