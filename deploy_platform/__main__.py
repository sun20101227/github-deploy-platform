"""Stable local process supervisor; exit 75 requests a fresh application process."""
import os
import subprocess
import sys

from . import config  # noqa: F401  loads .env before the settings below are read


def supervise(command):
    env = dict(os.environ, DEPLOY_SUPERVISED='1')
    while True:
        child = subprocess.Popen(command, env=env)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            child.terminate()
            child.wait(timeout=15)
            return 0
        if code != 75:
            return code
        # A self update may add dependencies. Install before importing the new API.
        result = subprocess.run([sys.executable, '-m', 'pip', 'install', '-e', os.path.dirname(os.path.dirname(__file__))])
        if result.returncode:
            return result.returncode


def admin_command(argv):
    """python -m deploy_platform admin <用户名>: create the admin, or reset a forgotten password."""
    import getpass
    from . import auth, db
    if len(argv) != 1:
        print('用法：python -m deploy_platform admin <用户名>', file=sys.stderr)
        return 2
    db.init()
    auth.init()
    password = getpass.getpass('新密码：')
    if password != getpass.getpass('再输一次：'):
        print('两次输入的密码不一样', file=sys.stderr)
        return 1
    try:
        auth.set_admin(argv[0], password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f'已设置管理员 {argv[0]}；这个账户之前的登录都已失效')
    return 0


if __name__ == '__main__':
    if sys.argv[1:2] == ['admin']:
        sys.exit(admin_command(sys.argv[2:]))
    sys.exit(supervise([sys.executable, '-m', 'uvicorn', 'deploy_platform.api:app',
                       '--host', os.getenv('DEPLOY_HOST','127.0.0.1'),
                       '--port', os.getenv('DEPLOY_PORT','8000')]))
