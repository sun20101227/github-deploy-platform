"""Use the service user's GitHub CLI identity without copying its token to SQLite."""
import json
import subprocess


def run(*args):
    try:
        result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('无法调用 gh；请用运行服务的用户安装并登录 GitHub CLI') from None
    if result.returncode:
        raise RuntimeError('gh 未登录或授权已失效，请在服务器运行 gh auth login --hostname github.com')
    return result.stdout.strip()


def identity():
    user = json.loads(run('api', '--hostname', 'github.com', 'user'))
    return {'github_id': str(user['id']), 'login': user['login']}


def token(value):
    if not value.startswith('gh-cli:'):
        return value
    if identity()['github_id'] != value.split(':', 1)[1]:
        raise RuntimeError('服务器 gh 账号已更换，请在 GitHub 页面重新连接')
    return run('auth', 'token', '--hostname', 'github.com')
