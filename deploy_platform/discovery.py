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


def source_repository(labels):
    for key in ('org.opencontainers.image.source', 'org.opencontainers.image.url'):
        name = repository_name((labels or {}).get(key, '').strip())
        if name:
            return name
    return ''


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
                           'files': labels.get('com.docker.compose.project.config_files', '').split(','),
                           # Images built in CI usually say where their code lives (OCI label).
                           'source': source_repository(labels)})
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
            # systemd prints "!/path" for optional directories; keep only real absolute paths
            if data.get('WorkingDirectory', '').startswith('/'):
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
        if p != base and p.resolve() == config.ROOT.resolve():
            dirs[:] = []
            continue
        # A Git checkout, a release layout, or a directory something on this server runs from.
        # "Runs from here" means the app's own folder, not a parent like /opt/1panel that holds several.
        runs_here = any(_app_path(x) and app_root(x).resolve() == p.resolve()
                        for x in [c['path'] for c in inventory] + [u['path'] for u in units])
        if (p/'.git').exists() or _release_layout(p) or (p != base and (
                runs_here or any((p/f).is_file() for f in COMPOSE_FILES))):
            dirs[:] = []
            try:
                found.append(_attach(describe(p), inventory, units))
            except (RuntimeError, OSError):
                continue
    return {'projects': found, 'containers': inventory, 'warnings': warnings}


# Paths that belong to the system, not to an application someone deployed.
SYSTEM_PREFIXES = ('/usr', '/etc', '/var/lib', '/var/cache', '/var/log', '/run', '/proc', '/sys', '/boot',
                   '/dev', '/snap', '/lib', '/lib64', '/bin', '/sbin', '/tmp')
# Stop walking up at these: the app is somewhere below them.
APP_PARENTS = {'/', '/opt', '/srv', '/home', '/var', '/var/www', '/www', '/root', '/data'}


def _app_path(value):
    if not value or str(value).startswith('!'):
        return False
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        return False
    text = path.as_posix()
    return not any(text == p or text.startswith(p + '/') for p in SYSTEM_PREFIXES)


def _under(child, root):
    """True when child is root or inside it, comparing both as written and with symlinks resolved."""
    if not child:
        return False
    child, root = Path(child), Path(root)
    pairs = [(Path(os.path.abspath(child)), Path(os.path.abspath(root)))]
    try:
        pairs.append((child.resolve(), root.resolve()))
    except OSError:
        pass
    return any(c == r or r in c.parents for c, r in pairs)


def _bare_repository(root):
    """A bare repository kept beside the releases, such as qiyu-diary's repo.git."""
    for candidate in sorted(root.glob('*.git')):
        if candidate.is_dir() and (candidate/'HEAD').is_file() and (candidate/'objects').is_dir():
            return candidate
    return None


def _release_layout(root):
    return (root/'releases').is_dir() and (root/'current').exists()


def app_root(value):
    """Walk up from a working directory to the folder that holds the whole app."""
    start = Path(os.path.abspath(value))
    for candidate in [start, *start.parents][:5]:
        if candidate.as_posix() in APP_PARENTS or candidate == Path(candidate.anchor):
            break
        if (candidate/'.git').exists() or _release_layout(candidate) or _bare_repository(candidate):
            return candidate
    return start


def describe(path):
    """Like inspect(), but also for apps that are not a Git checkout. Reads only."""
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError('不是目录')
    if root == config.ROOT.resolve():
        raise RuntimeError('平台本体请在平台更新页管理')
    item = {'path': str(root), 'name': root.name, 'kind': 'plain', 'repository': '', 'branch': '', 'commit': '',
            'dirty': False, 'note': '', 'compose_files': [str(root/f) for f in COMPOSE_FILES if (root/f).is_file()]}
    if (root/'.git').exists():
        try:
            return {**item, **inspect(root), 'kind': 'git'}
        except RuntimeError as exc:
            # e.g. a checkout git refuses to read; say so instead of hiding the app
            return {**item, 'kind': 'git', 'note': f'Git 仓库读取失败：{exc}'}
    bare = _bare_repository(root)
    if bare:
        try:
            with Repo(bare) as repo:
                # Without an origin the bare repo only receives pushes; its HEAD branch says nothing then.
                if 'origin' in repo.remotes:
                    item['repository'] = repository_name(repo.remotes.origin.url)
                    item['branch'] = '' if repo.head.is_detached else repo.active_branch.name
        except Exception:
            pass
    if _release_layout(root):
        item['kind'] = 'releases'
        item['note'] = '有自己的版本目录（releases/current），通常也有自己的更新程序；接入后填它的更新命令。'
    elif root.is_relative_to('/opt/1panel'):
        item['note'] = '由 1Panel 管理的应用，建议继续在 1Panel 里更新。'
    else:
        item['note'] = '不是 Git 仓库。接入后平台不改动这里的文件，更新时执行构建和重启。'
    return item


def _attach(item, inventory, units):
    root = item['path']
    item['containers'] = [c for c in inventory if _under(c['path'], root)]
    item['services'] = [u for u in units if _under(u['path'], root)]
    item.setdefault('repository_from', 'directory' if item['repository'] else '')
    if not item['repository']:
        sources = [c.get('source') for c in item['containers'] if c.get('source')]
        if sources:
            # Most common source among the app's containers (the app image, not postgres or redis).
            item['repository'] = max(set(sources), key=sources.count)
            item['repository_from'] = 'image'
    return item


def running():
    """Start from what runs here: Compose working directories and systemd WorkingDirectory."""
    inventory, warning = containers()
    units = services()
    found, seen = [], set()
    for value in [c['path'] for c in inventory] + [u['path'] for u in units]:
        if not _app_path(value) or value in seen or len(seen) >= 300:
            continue
        seen.add(value)
        try:
            item = describe(app_root(value))
        except (RuntimeError, OSError):
            continue  # gone, unreadable, or the platform itself
        if item['path'] == str(Path(item['path']).anchor) or any(f['path'] == item['path'] for f in found):
            continue
        found.append(_attach(item, inventory, units))
    # Git checkouts first, then apps with a GitHub repository, then the rest.
    found.sort(key=lambda p: (p['kind'] != 'git', not p['repository'], p['name']))
    return {'projects': found, 'containers': inventory, 'warnings': [warning] if warning else []}
