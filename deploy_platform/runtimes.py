"""Adapters delegate container lifecycle to Compose and native services to systemd."""
import json
import subprocess
from pathlib import Path

from python_on_whales import DockerClient
from . import discovery


def validate(runtime, settings, root):
    root = Path(root).resolve()
    if runtime == 'compose':
        files = settings.get('files', [])
        name = settings.get('project', '')
        import re
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
        if not directory or Path(directory).resolve() != root:
            raise RuntimeError('服务 WorkingDirectory 与项目目录不一致，请使用自定义重启命令')
        return {'unit': unit}
    if runtime == 'command':
        return {}
    raise RuntimeError('不支持的运行方式')


def restart(project, target):
    settings = json.loads(project.get('runtime_config') or '{}')
    runtime = project.get('runtime', 'command')
    if runtime == 'compose':
        settings = validate(runtime, settings, target)
        client = DockerClient(compose_files=settings['files'], compose_project_name=settings['project'],
                              compose_project_directory=str(target))
        # Keep the original project name so volumes/network/container ownership stay stable.
        client.compose.up(detach=True, build=True, force_recreate=True, wait=True, wait_timeout=90)
        return True
    if runtime == 'systemd':
        settings = validate(runtime, settings, target)
        subprocess.run(['systemctl', 'restart', settings['unit']], timeout=120, check=True, capture_output=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', settings['unit']], timeout=10, check=True)
        return True
    return False
