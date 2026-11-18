"""Adapters delegate container lifecycle to Compose and native services to systemd."""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from python_on_whales import DockerClient
from python_on_whales.exceptions import DockerException
from . import discovery


def validate(runtime, settings, root):
    root = Path(root).resolve()
    if runtime == 'compose':
        files = settings.get('files', [])
        name = settings.get('project', '')
        if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name) or not isinstance(files,list) or not files:
            raise RuntimeError('请填写原 Compose 项目名和配置文件')
        checked = []
        for file in files:
            if not isinstance(file,str):
                raise RuntimeError('Compose 配置文件路径无效')
            try:
                p = (root / file).resolve(strict=True)
            except OSError:
                raise RuntimeError('Compose 配置文件不存在或无法读取') from None
            if not p.is_file() or not p.is_relative_to(root):
                raise RuntimeError('Compose 配置必须在项目目录内')
            checked.append(str(p))
        return {'project': name, 'files': checked}
    if runtime == 'systemd':
        unit = settings.get('unit', '')
        if not isinstance(unit,str):
            raise RuntimeError('systemd 服务名无效')
        directory = discovery.service_directory(unit)
        # Inside is enough: release layouts run from e.g. <root>/current/.next/standalone.
        if not directory or not discovery._under(directory, root):
            raise RuntimeError('服务的 WorkingDirectory 不在项目目录里，请使用自定义重启命令')
        return {'unit': unit}
    if runtime == 'command':
        return {}
    raise RuntimeError('不支持的运行方式')


def validate_release(runtime, settings, repository):
    """Settings for a project deployed into releases/<job>: the files do not exist yet,
    so only their shape is checked here; restart() checks them inside each release."""
    if runtime == 'command':
        return {}
    if runtime != 'compose':
        raise RuntimeError('新建项目支持 Docker Compose 或自定义命令；systemd 服务请用命令 systemctl restart')
    name = settings.get('project') or re.sub(r'[^a-z0-9_-]+', '-', repository.split('/')[-1].lower()).strip('-_')
    files = settings.get('files') or []
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name):
        raise RuntimeError('Compose 项目名只能用小写字母、数字、横线和下划线')
    if not isinstance(files, list) or any(
            not isinstance(f, str) or not f or Path(f).is_absolute() or f.startswith(('/', '\\')) or '..' in Path(f).parts
            for f in files):
        raise RuntimeError('Compose 文件请填写仓库内的相对路径')
    return {'project': name, 'files': files}


def restart(project, target):
    settings = json.loads(project.get('runtime_config') or '{}')
    runtime = project.get('runtime', 'command')
    if runtime == 'compose':
        target = Path(target)
        if not settings.get('files'):
            found = [f for f in discovery.COMPOSE_FILES if (target / f).is_file()][:1]
            if not found:
                raise RuntimeError('仓库里没有找到 compose.yaml；请在项目设置里填写 Compose 文件，或改用自定义命令')
            settings = {**settings, 'files': found}
        settings = validate(runtime, settings, target)
        # Releases are fresh clones without the ignored .env; share one kept beside them.
        shared = Path(project.get('deploy_root') or target) / '.env'
        env_files = [str(shared)] if project.get('mode') == 'release' and shared.is_file() else []
        client = DockerClient(compose_files=settings['files'], compose_project_name=settings['project'],
                              compose_project_directory=str(target), compose_env_files=env_files)
        # Keep the original project name so volumes/network/container ownership stay stable.
        try:
            client.compose.up(detach=True, build=True, force_recreate=True, wait=True, wait_timeout=90)
        except DockerException as exc:
            # Compose output is usually not captured; it goes to the platform's own service log.
            detail = (exc.stderr or exc.stdout or '').strip()[-1200:]
            raise RuntimeError(f'Compose 启动失败（退出码 {exc.return_code}）' +
                               ('：' + detail if detail else '，详细输出在平台服务日志里')) from None
        return True
    if runtime == 'systemd':
        settings = validate(runtime, settings, target)
        unit = settings['unit']
        try:
            subprocess.run(['systemctl', 'restart', unit], timeout=120, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or '').strip()[-800:]
            raise RuntimeError(f'systemctl restart {unit} 失败' + ('：' + detail if detail else '')) from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f'systemctl restart {unit} 超过 120 秒未完成') from None
        try:
            subprocess.run(['systemctl', 'is-active', '--quiet', unit], timeout=10, check=True)
        except subprocess.SubprocessError:
            raise RuntimeError(f'{unit} 重启后未处于运行状态，请查看 journalctl -u {unit}') from None
        return True
    return False


# ---- Is it running, and what is it printing? Read only. ----

def _compose_containers(name):
    import docker
    client = docker.from_env(timeout=5)
    try:
        return client, client.containers.list(all=True, filters={'label': f'com.docker.compose.project={name}'})
    except Exception:
        client.close()
        raise


def _healthy(url):
    import httpx
    try:
        return 200 <= httpx.get(url, timeout=2, follow_redirects=False).status_code < 300
    except httpx.HTTPError:
        return False


def status(project):
    """{'state': running|degraded|stopped|unknown, 'detail': text, 'items': [...], 'health': True/False/None}."""
    settings = json.loads(project.get('runtime_config') or '{}')
    runtime = project.get('runtime', 'command')
    items, note = [], ''
    try:
        if runtime == 'compose' and settings.get('project'):
            client, containers = _compose_containers(settings['project'])
            try:
                for c in containers:
                    state = c.attrs.get('State', {})
                    items.append({'name': c.name, 'state': c.status, 'health': (state.get('Health') or {}).get('Status', ''),
                                  'since': state.get('StartedAt', '') if c.status == 'running' else state.get('FinishedAt', ''),
                                  'restarts': c.attrs.get('RestartCount', 0)})
            finally:
                client.close()
        elif runtime == 'systemd' and settings.get('unit'):
            props = ['systemctl', 'show', settings['unit'], '--property=ActiveState,SubState,ActiveEnterTimestamp,NRestarts']
            # "@1731890000" instead of "Wed 2026-11-18 00:33:18 CST", so the page can say "3 小时前"
            r = subprocess.run(props + ['--timestamp=unix'], capture_output=True, text=True, timeout=5)
            if r.returncode:  # systemd older than 247
                r = subprocess.run(props, capture_output=True, text=True, timeout=5)
            data = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
            active = data.get('ActiveState', 'unknown')
            since = data.get('ActiveEnterTimestamp', '')
            if since.startswith('@') and since[1:].isdigit():
                since = datetime.fromtimestamp(int(since[1:]), timezone.utc).isoformat()
            items.append({'name': settings['unit'], 'state': 'running' if active == 'active' else active,
                          'health': data.get('SubState', ''), 'since': since,
                          'restarts': int(data.get('NRestarts') or 0)})
    except Exception:
        note = 'Docker 未运行或当前用户无访问权限' if runtime == 'compose' else '读不到服务状态'
    health = _healthy(project['healthcheck_url']) if project.get('healthcheck_url') else None
    if items:
        running = [i for i in items if i['state'] == 'running' and i['health'] not in ('unhealthy',)]
        state = 'running' if len(running) == len(items) else 'degraded' if running else 'stopped'
    elif note:
        state = 'unknown'
    elif runtime == 'compose':
        state, note = 'stopped', '没有找到这个 Compose 项目的容器'
    else:
        # A custom command leaves nothing to look at; the health check is the only signal.
        state = 'unknown' if health is None else 'running' if health else 'stopped'
        note = '自定义命令的项目只能通过健康检查判断' if health is None else ''
    if state == 'running' and health is False:
        state, note = 'degraded', '进程在运行，但健康检查没有通过'
    return {'state': state, 'detail': note, 'items': items, 'health': health}


def logs(project, lines=200):
    """Recent output of the service itself (not of deployments)."""
    settings = json.loads(project.get('runtime_config') or '{}')
    runtime = project.get('runtime', 'command')
    if runtime == 'systemd' and settings.get('unit'):
        r = subprocess.run(['journalctl', '-u', settings['unit'], '-n', str(lines), '--no-pager', '-o', 'short-iso'],
                           capture_output=True, text=True, timeout=15)
        return {'source': f"journalctl -u {settings['unit']}", 'text': r.stdout or r.stderr or '没有日志'}
    if runtime == 'compose' and settings.get('project'):
        client, containers = _compose_containers(settings['project'])
        try:
            parts = []
            for c in sorted(containers, key=lambda c: c.name):
                text = c.logs(tail=lines, timestamps=True).decode('utf-8', errors='replace')
                parts.append(f'== {c.name} ==\n{text}' if len(containers) > 1 else text)
        finally:
            client.close()
        return {'source': f"docker compose -p {settings['project']} logs", 'text': '\n'.join(parts) or '没有日志'}
    raise RuntimeError('自定义命令的项目看不到服务日志：平台不知道它把输出写到了哪里')
