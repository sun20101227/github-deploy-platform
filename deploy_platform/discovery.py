"""Bounded, read-only inventory. Never return environment variables or remote credentials."""
import os
import re
import subprocess
from pathlib import Path

import docker
from git import Repo, InvalidGitRepositoryError, NoSuchPathError

from . import config

COMPOSE_FILES = ('compose.yaml', 'compose.yml', 'docker-compose.yml', 'docker-compose.yaml')
SKIP = {'.git', '.venv', 'venv', 'node_modules', 'data', 'releases', '.updates', '__pycache__'}


def repository_name(url):
    match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?', url)
    return match[1] if match else ''


def inspect(path):
    root = Path(path).resolve(strict=True)
    if root == config.ROOT.resolve():
        raise RuntimeError('平台本体请在平台更新页管理')
    try:
        with Repo(root) as repo:
            if repo.bare or Path(repo.working_tree_dir).resolve() != root:
                raise RuntimeError('请选择 Git 项目的根目录')
            remote = repository_name(repo.remotes.origin.url) if 'origin' in repo.remotes else ''
            return {'path': str(root), 'name': root.name, 'repository': remote,
                    'branch': '' if repo.head.is_detached else repo.active_branch.name,
                    'commit': repo.head.commit.hexsha, 'dirty': repo.is_dirty(untracked_files=True),
                    'compose_files': [str(root/f) for f in COMPOSE_FILES if (root/f).is_file()]}
    except (InvalidGitRepositoryError, NoSuchPathError, ValueError):
        raise RuntimeError('未找到有效的 Git 仓库') from None


def containers():
    client = None
    try:
        client = docker.from_env(timeout=3)
        result = []
        for container in client.containers.list(all=True):
            labels = container.labels or {}
            result.append({'name': container.name, 'status': container.status,
                           'project': labels.get('com.docker.compose.project', ''),
                           'path': labels.get('com.docker.compose.project.working_dir', ''),
                           'files': labels.get('com.docker.compose.project.config_files', '').split(',')})
        return result, ''
    except Exception:
        return [], 'Docker 未运行或当前用户无访问权限'
    finally:
        if client:
            client.close()


def service_directory(unit):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', unit) or unit.startswith('-'):
        raise RuntimeError('systemd 服务名无效')
    try:
        r = subprocess.run(['systemctl','show',unit,'--property=WorkingDirectory','--value'],
                           capture_output=True, text=True, timeout=5, check=True)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('无法读取 systemd 服务') from None


def services():
    try:
        r = subprocess.run(['systemctl','show','--all','--property=Id,WorkingDirectory,ActiveState','*.service'],
                           capture_output=True,text=True,timeout=8,check=True)
        items=[]
        for block in r.stdout.strip().split('\n\n'):
            data=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            if data.get('WorkingDirectory'):
                items.append({'unit':data.get('Id',''),'path':data['WorkingDirectory'],'status':data.get('ActiveState','unknown')})
        return items
    except (OSError,subprocess.SubprocessError):
        return []


def scan(root, depth=2):
    base = Path(root).resolve(strict=True)
    if not base.is_dir() or base == Path(base.anchor):
        raise RuntimeError('请选择项目所在目录，不支持扫描整个磁盘')
    found, warnings, visited = [], [], 0
    inventory, warning = containers()
    units = services()
    if warning:
        warnings.append(warning)
    for directory, dirs, _ in os.walk(base, followlinks=False):
        visited += 1
        p = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (p/d).is_symlink())
        if len(p.relative_to(base).parts) >= depth:
            dirs[:] = []
        if visited > 500 or len(found) >= 100:
            warnings.append('已达到扫描上限，请缩小目录范围')
            break
        if (p/'.git').exists():
            dirs[:] = []
            try:
                found.append(_attach(inspect(p), inventory, units))
            except (RuntimeError, OSError):
                continue
    return {'projects': found, 'containers': inventory, 'warnings': warnings}


def _attach(item, inventory, units):
    root = Path(item['path'])
    item['containers'] = [c for c in inventory if c['path'] and Path(c['path']).resolve() == root]
    item['services'] = [u for u in units if Path(u['path']).resolve() == root]
    return item


def running():
    """Start from what runs here: Compose working directories and systemd WorkingDirectory."""
    inventory, warning = containers()
    units = services()
    found, seen = [], set()
    for value in [c['path'] for c in inventory] + [u['path'] for u in units]:
        if not value or value in seen or len(seen) >= 300:
            continue
        seen.add(value)
        try:
            path = Path(value).resolve(strict=True)
            if path == Path(path.anchor) or any(f['path'] == str(path) for f in found):
                continue
            found.append(_attach(inspect(path), inventory, units))
        except (RuntimeError, OSError):
            continue  # not a Git checkout, unreadable, or the platform itself
    return {'projects': found, 'containers': inventory, 'warnings': [warning] if warning else []}
