import os
import threading


def supported():
    return os.environ.get('DEPLOY_SUPERVISED') == '1'


def schedule():
    if not supported():
        raise RuntimeError('请使用 python -m deploy_platform 启动，才能在更新后自动重启')
    timer = threading.Timer(1.5, lambda: os._exit(75))
    timer.daemon = True
    timer.start()
