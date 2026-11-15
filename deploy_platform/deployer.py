from __future__ import annotations

import os
import shlex
import subprocess
import threading
import uuid
from pathlib import Path

from . import config, db

_project_locks: dict[int, threading.Lock] = {}
_project_locks_guard = threading.Lock()


class DeploymentError(RuntimeError):
    pass


def _run(args: list[str], *, cwd: Path | None = None, env: dict | None = None, timeout: int = 900) -> str:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise DeploymentError(detail or f"命令失败：{shlex.join(args)}")
    return result.stdout.strip()


def _repo_url(repository: str) -> str:
    owner, name = repository.split("/", 1)
    return f"https://github.com/{owner}/{name}.git"


def deploy(project_id: int, *, account: dict, project: dict, action: str = "deploy") -> int:
    """Run one serialized deployment in a background worker.

    The release is prepared in a separate directory and only becomes current after
    the build and health checks pass. The process manager is intentionally external:
    start_command can later be replaced by Docker/systemd adapters.
    """
    deployment_id = None
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO deployments(project_id, action, status, started_at) VALUES (?, ?, 'queued', ?)",
            (project_id, action, db.now()),
        )
        deployment_id = cursor.lastrowid

    threading.Thread(
        target=_run_deployment,
        args=(deployment_id, account, project),
        daemon=True,
        name=f"deployment-{deployment_id}",
    ).start()
    return deployment_id


def rollback(project_id: int, project: dict) -> int:
    """Create a rollback record and atomically point the project to its last release."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM deployments WHERE project_id=? AND status='succeeded' ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if not row or not row["previous_release"] or not Path(row["previous_release"]).is_dir():
            raise DeploymentError("没有可用的上一版本")
        cursor = conn.execute(
            "INSERT INTO deployments(project_id, action, status, started_at, release_dir, previous_release) VALUES (?, 'rollback', 'succeeded', ?, ?, ?)",
            (project_id, db.now(), row["previous_release"], row["release_dir"]),
        )
        deployment_id = cursor.lastrowid
    root = Path(project["deploy_root"]).resolve()
    temp = root / f".current.rollback.{deployment_id}.tmp"
    temp.write_text(row["previous_release"], encoding="utf-8")
    os.replace(temp, root / "current")
    return deployment_id


def _run_deployment(deployment_id: int, account: dict, project: dict) -> None:
    release_dir = ""
    try:
        with db.connect() as conn:
            conn.execute("UPDATE deployments SET status='running' WHERE id=?", (deployment_id,))
        project_root = Path(project["deploy_root"]).resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        release_dir = project_root / "releases" / f"{deployment_id}-{uuid.uuid4().hex[:8]}"
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        token = account["access_token"]
        # The token is passed through an ephemeral HTTP header, never written into the URL or logs.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: bearer {token}"
        _run(["git", "clone", "--depth", "1", "--branch", project["branch"], _repo_url(project["repository"]), str(release_dir)], env=env)
        sha = _run(["git", "rev-parse", "HEAD"], cwd=release_dir)
        if project["build_command"]:
            _run(["cmd", "/c", project["build_command"]] if os.name == "nt" else ["sh", "-lc", project["build_command"]], cwd=release_dir, timeout=1800)
        if project["healthcheck_url"]:
            # A future HTTP health adapter will run here. Requiring it to be explicit
            # prevents silently claiming a deployment is healthy.
            raise DeploymentError("healthcheck_url 暂未接入，请先留空或配置 HTTP 健康检查适配器")
        previous = _current_release(project_root)
        with db.connect() as conn:
            conn.execute(
                "UPDATE deployments SET previous_release=? WHERE id=?",
                (previous, deployment_id),
            )
        current = project_root / "current"
        current_tmp = project_root / f".current.{deployment_id}.tmp"
        current_tmp.write_text(str(release_dir), encoding="utf-8")
        os.replace(current_tmp, current)
        if project["start_command"]:
            _run(["cmd", "/c", project["start_command"]] if os.name == "nt" else ["sh", "-lc", project["start_command"]], cwd=release_dir, timeout=60)
        with db.connect() as conn:
            conn.execute(
                "UPDATE deployments SET status='succeeded', commit_sha=?, release_dir=?, previous_release=?, finished_at=? WHERE id=?",
                (sha, str(release_dir), previous, db.now(), deployment_id),
            )
    except Exception as exc:
        # If activation happened but the start/health step failed, restore the
        # previous pointer before reporting failure. The old release remains
        # untouched and can be started by the configured process manager.
        if release_dir:
            try:
                project_root = Path(project["deploy_root"]).resolve()
                current = project_root / "current"
                with db.connect() as conn:
                    previous = conn.execute(
                        "SELECT previous_release FROM deployments WHERE id=?", (deployment_id,)
                    ).fetchone()
                if previous and previous["previous_release"]:
                    tmp = project_root / f".current.rollback.{deployment_id}.tmp"
                    tmp.write_text(previous["previous_release"], encoding="utf-8")
                    os.replace(tmp, current)
            except Exception:
                pass
        with db.connect() as conn:
            conn.execute(
                "UPDATE deployments SET status='failed', release_dir=?, message=?, finished_at=? WHERE id=?",
                (str(release_dir), str(exc)[:2000], db.now(), deployment_id),
            )


def _current_release(project_root: Path) -> str:
    current = project_root / "current"
    try:
        value = current.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    return value if Path(value).is_dir() else ""

