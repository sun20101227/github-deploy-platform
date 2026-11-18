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


def _host_ports(attrs):
    ports = set()
    for bindings in ((attrs.get('NetworkSettings') or {}).get('Ports') or {}).values():
        for b in bindings or []:
            if str(b.get('HostPort', '')).isdigit():
                ports.add(int(b['HostPort']))
    return sorted(ports)


def _mount_app_dir(sources):
    """/opt/custom-auth/data -> /opt/custom-auth: the app folder right below /opt, /srv, ..."""
    for source in sources:
        path = Path(source)
        if not _app_path(source):
            continue
        for parent in [path, *path.parents]:
            if parent.parent.as_posix() in APP_PARENTS and parent.parent != parent:
                return parent.as_posix()
    return ''


def containers():
    client = None
    try:
        client = docker.from_env(timeout=3)
        result = []
        for container in client.containers.list(all=True):
            labels = container.labels or {}
            attrs = container.attrs or {}
            mounts = [m.get('Source', '') for m in attrs.get('Mounts') or [] if m.get('Type') == 'bind']
            project = labels.get('com.docker.compose.project', '')
            result.append({'name': container.name, 'id': container.id or '', 'status': container.status,
                           'project': project,
                           'path': labels.get('com.docker.compose.project.working_dir', ''),
                           'files': labels.get('com.docker.compose.project.config_files', '').split(','),
                           'image': (attrs.get('Config') or {}).get('Image', ''),
                           'ports': _host_ports(attrs),
                           # Started with docker run: the folder it keeps its data in is the app's folder.
                           'app_dir': '' if project else _mount_app_dir(mounts),
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


SYSTEMD_DIRS = ['/etc/systemd/system']


def _unit_files():
    for d in SYSTEMD_DIRS:
        p = Path(d)
        if p.is_dir():
            yield from sorted(f for f in p.glob('*.service') if f.is_file())


def _read(path, limit=256 * 1024):
    try:
        p = Path(path)
        if p.is_file() and p.stat().st_size <= limit:
            return p.read_text(encoding='utf-8', errors='replace')
    except OSError:
        pass
    return ''


def services():
    items, seen = [], set()
    try:
        r = subprocess.run(['systemctl','show','--all','--property=Id,WorkingDirectory,ActiveState','*.service'],
                           capture_output=True,text=True,timeout=8,check=True)
        for block in r.stdout.strip().split('\n\n'):
            data=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            seen.add(data.get('Id', ''))
            # systemd prints "!/path" for optional directories; keep only real absolute paths
            wd = data.get('WorkingDirectory', '')
            if wd and not wd.startswith('!') and Path(wd).is_absolute():
                items.append({'unit':data.get('Id',''),'path':data['WorkingDirectory'],'status':data.get('ActiveState','unknown')})
    except (OSError,subprocess.SubprocessError):
        pass
    # Units that are installed but not loaded (stopped and not enabled) do not show up above.
    for f in _unit_files():
        if f.name in seen:
            continue
        m = re.search(r'^\s*WorkingDirectory\s*=\s*-?(\S+)', _read(f), re.M)
        if m and Path(m[1]).is_absolute():
            items.append({'unit': f.name, 'path': m[1], 'status': 'inactive'})
    return items


# ---- Which domain and port belong to which app ----

# Site configs of nginx/OpenResty on the host and of 1Panel's OpenResty container.
NGINX_DIRS = ['/etc/nginx/conf.d', '/etc/nginx/sites-enabled', '/opt/1panel/www/conf.d']
# 1Panel's OpenResty sees /opt/1panel/www as /www; its include paths are written that way.
CONTAINER_PATHS = {'/www/': '/opt/1panel/www/'}
_LOCAL = r'(?:127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])'


def _host_path(path):
    for inside, host in CONTAINER_PATHS.items():
        if path.startswith(inside) and not Path(path).parent.exists():
            return host + path[len(inside):]
    return path


def sites():
    """[{'domains': [...], 'ports': [...]}] from server_name and proxy_pass to a local port."""
    import glob
    found = []
    for d in NGINX_DIRS:
        if not Path(d).is_dir():
            continue
        for conf in sorted(Path(d).glob('*.conf'))[:200]:
            text = _read(conf)
            # 1Panel sometimes writes a whole server block on one line, so "include" need not start a line.
            for inc in re.findall(r'(?:^|[;{\s])include\s+([^;]+);', text, re.M)[:20]:
                for f in sorted(glob.glob(_host_path(inc.strip())))[:20]:
                    text += '\n' + _read(f)
            names = sorted({n for m in re.findall(r'server_name\s+([^;]+);', text) for n in m.split()
                            if '.' in n and not n.startswith(('_', '~', '*'))})
            ports = sorted({int(p) for p in re.findall(rf'proxy_pass\s+https?://{_LOCAL}:(\d+)', text)})
            if names and ports:
                found.append({'domains': names, 'ports': ports})
    return found


def listeners():
    """{port: systemd unit} for local TCP listeners owned by a system service (not by Docker's proxy)."""
    try:
        r = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    owners = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or not (m := re.search(r':(\d+)$', parts[3])):
            continue
        for pid in re.findall(r'pid=(\d+)', line)[:1]:
            unit = re.search(r'/([^/\s]+\.service)\s*$', _read(f'/proc/{pid}/cgroup'), re.M)
            if unit and unit[1] != 'docker.service':
                owners[int(m[1])] = unit[1]
    return owners


def _endpoints(item_containers, item_units, all_sites, owners):
    ports = {p for c in item_containers for p in c.get('ports', [])}
    units = {u['unit'] for u in item_units}
    ports |= {p for p, unit in owners.items() if unit in units}
    domains = sorted({d for s in all_sites if ports & set(s['ports']) for d in s['domains']})
    return sorted(ports), domains


# ---- Where the code comes from, and how the app updates itself ----

_GITHUB = re.compile(r'github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[\s"\'<>)\]#?]|$)', re.M)


def _fetch_head(bare):
    """A bare repo the updater fetches into remembers where from: "... branch 'main' of github.com:owner/repo"."""
    m = re.search(r"branch '([^']+)' of (?:https://)?github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s*$", _read(bare/'FETCH_HEAD'), re.M)
    return (m[2], m[1]) if m else ('', '')


def _updaters(root):
    """One-shot units that update this app, e.g. qiyu-updater.service whose script works on /opt/qiyu-diary.
    Returns [(unit, text read from the unit, its env files and its script)]."""
    found = []
    for f in _unit_files():
        unit = _read(f)
        if not re.search(r'updat|upgrad|deploy|release', f.stem, re.I) or not re.search(r'^\s*Type\s*=\s*oneshot', unit, re.M):
            continue
        text = unit
        for env in re.findall(r'^\s*EnvironmentFile\s*=\s*-?(\S+)', unit, re.M):
            text += '\n' + _read(env)
        for exe in re.findall(r'^\s*ExecStart\s*=\s*[-@:+!]*(\S+)', unit, re.M):
            if Path(exe).is_absolute():
                text += '\n' + _read(exe)
        if str(root) in text or Path(root).as_posix() in text:
            found.append((f.name, text))
    return found


def _mentioned_repository(texts, names):
    """A GitHub repository named like the app in its own config or updater; ignores dependencies."""
    names = {n.lower() for n in names if n}
    for text in texts:
        for owner, repo in _GITHUB.findall(text):
            if repo.lower() in names:
                return f'{owner}/{repo}'
    return ''


def scan(root, depth=2):
    base = Path(root).resolve(strict=True)
    if not base.is_dir() or base == Path(base.anchor):
        raise RuntimeError('请选择项目所在目录，不支持扫描整个磁盘')
    found, warnings, visited = [], [], 0
    inventory, warning = containers()
    units = services()
    all_sites, owners = sites(), listeners()
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
                        for x in [c['path'] or c.get('app_dir', '') for c in inventory] + [u['path'] for u in units])
        if (p/'.git').exists() or _release_layout(p) or (p != base and (
                runs_here or any((p/f).is_file() for f in COMPOSE_FILES))):
            dirs[:] = []
            try:
                found.append(_attach(describe(p), inventory, units, all_sites, owners))
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
            'repository_from': '', 'updater': '', 'dirty': False, 'note': '',
            'compose_files': [str(root/f) for f in COMPOSE_FILES if (root/f).is_file()]}
    if (root/'.git').exists():
        try:
            found = inspect(root)
            return {**item, **found, 'kind': 'git', 'repository_from': 'directory' if found['repository'] else ''}
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
        if not item['repository']:
            item['repository'], item['branch'] = _fetch_head(bare)
        if item['repository']:
            item['repository_from'] = 'directory'
    updaters = _updaters(root)
    item['updater'] = updaters[0][0] if updaters else ''
    if not item['repository']:
        # The app's own updater config or deploy notes, e.g. REPO_URL=https://github.com/owner/<app>.git
        texts = [t for _, t in updaters] + [_read(root/f) for f in ('DEPLOY.md', 'README.md', 'package.json', '.env.example')]
        if (repo := _mentioned_repository(texts, [root.name])):
            item['repository'], item['repository_from'] = repo, 'config'
    if _release_layout(root):
        item['kind'] = 'releases'
        item['note'] = (f'有自己的版本目录和更新程序 {item["updater"]}；接入时用它更新。' if item['updater']
                        else '有自己的版本目录（releases/current），通常也有自己的更新程序；接入后填它的更新命令。')
    elif root.is_relative_to('/opt/1panel'):
        item['note'] = '由 1Panel 管理的应用，建议继续在 1Panel 里更新。'
    else:
        item['note'] = '不是 Git 仓库。接入后平台不改动这里的文件，更新时执行构建和重启。'
    return item


def _attach(item, inventory, units, all_sites=(), owners=None):
    root = item['path']
    item['containers'] = [c for c in inventory if _under(c['path'], root) or (not c['project'] and c.get('app_dir') and _under(c['app_dir'], root))]
    item['services'] = [u for u in units if _under(u['path'], root)]
    item['ports'], item['domains'] = _endpoints(item['containers'], item['services'], all_sites, owners or {})
    item['image'] = ''
    sourced = [c for c in item['containers'] if c.get('source')]
    if sourced:
        # The app's own image is the one that names a source repository (postgres or redis usually don't).
        app = max(sourced, key=lambda c: sum(x.get('source') == c['source'] for x in sourced))
        item['image'] = app.get('image', '')
        if not item['repository']:
            item['repository'], item['repository_from'] = app['source'], 'image'
    if not item['repository'] and item['containers']:
        # Named like the app in the image name or its Dockerfile, e.g. a locally built qiyulab/openlist-auth
        texts = [_read(Path(root)/'Dockerfile')]
        names = [item['name'], *(c['project'] for c in item['containers']), *(c['name'] for c in item['containers'])]
        if (repo := _mentioned_repository(texts, names)):
            item['repository'], item['repository_from'] = repo, 'config'
    loose = [c for c in item['containers'] if not c['project']]
    if item['kind'] == 'plain' and item['containers'] and not item['path'].startswith('/opt/1panel'):
        image = item['image'] or item['containers'][0].get('image', '')
        if loose and len(loose) == len(item['containers']):
            # docker run, no Compose: the platform can restart it and read its logs, not recreate it.
            item['kind'] = 'container'
            item['note'] = (f'用 docker run 启动的容器 {"、".join(c["name"] for c in loose)}（镜像 {image}），这里放的是它的数据。'
                            '接入后可以看状态、看日志、重启；要换新镜像需要用原来的参数重新创建容器，平台做不了。')
        else:
            # No code here: the code ships inside the image, the directory only holds Compose config and data.
            item['kind'] = 'image'
            item['note'] = (f'代码不在这个目录里，打包在 Docker 镜像 {image} 中' if image else '代码打包在 Docker 镜像中') + \
                           '；目录里只有 Compose 配置和数据。接入后点“更新”会拉取最新镜像并重启。'
    if item['kind'] in ('plain',) and item['services'] and all(s['status'] != 'active' for s in item['services']):
        item['note'] = f'{"、".join(s["unit"] for s in item["services"])} 现在没有运行。' + item['note']
    return item


def running():
    """Start from what runs here: Compose working directories, docker run data folders and systemd WorkingDirectory."""
    inventory, warning = containers()
    units = services()
    all_sites, owners = sites(), listeners()
    found, seen = [], set()
    for value in [c['path'] or c.get('app_dir', '') for c in inventory] + [u['path'] for u in units]:
        if not _app_path(value) or value in seen or len(seen) >= 300:
            continue
        seen.add(value)
        try:
            item = describe(app_root(value))
        except (RuntimeError, OSError):
            continue  # gone, unreadable, or the platform itself
        if item['path'] == str(Path(item['path']).anchor) or any(f['path'] == item['path'] for f in found):
            continue
        found.append(_attach(item, inventory, units, all_sites, owners))
    # Git checkouts first, then apps with a GitHub repository, then the rest.
    found.sort(key=lambda p: (p['kind'] != 'git', not p['repository'], p['name']))
    return {'projects': found, 'containers': inventory, 'warnings': [warning] if warning else []}
