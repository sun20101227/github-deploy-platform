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


if __name__ == '__main__':
    sys.exit(supervise([sys.executable, '-m', 'uvicorn', 'deploy_platform.api:app',
                       '--host', os.getenv('DEPLOY_HOST','127.0.0.1'),
                       '--port', os.getenv('DEPLOY_PORT','8000')]))
