import json
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from deploy_platform import api, config, db, deployer, runtimes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATABASE', tmp_path/'svc.db')
    monkeypatch.setenv('DEPLOY_ADMIN_TOKEN', 'k')
    db.init()
    with db.connect() as c:
        c.execute("INSERT INTO github_accounts VALUES(1,'1','tester','fake',?,?)", (db.now(), db.now()))
    with TestClient(api.app, headers={'Authorization': 'Bearer k'}) as c:
        yield c


def job(project_id=1, action='deploy'):
    with db.connect() as c:
        return c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES(?,?,'queued',?)", (project_id, action, db.now())).lastrowid


def message(job_id):
    with db.connect() as c:
        return c.execute('SELECT message FROM deployments WHERE id=?', (job_id,)).fetchone()['message']


def test_build_output_appears_while_the_command_runs(client, tmp_path):
    j = job()
    script = tmp_path/'slow.py'
    script.write_text("import sys,time\nprint('step one',flush=True)\ntime.sleep(1.5)\nprint('step two',flush=True)\n")
    seen = []
    def watch():
        for _ in range(40):
            seen.append(message(j))
            if 'step two' in seen[-1]:
                return
            threading.Event().wait(0.1)
    watcher = threading.Thread(target=watch); watcher.start()
    assert deployer._run([sys.executable, str(script)], job=j) == ''
    watcher.join()
    # "step one" was in the record before the command finished.
    assert any('step one' in m and 'step two' not in m for m in seen)
    assert message(j) == 'step one\nstep two\n'


def test_failing_command_reports_exit_code_and_keeps_output(client, tmp_path):
    j = job()
    with pytest.raises(deployer.DeploymentError, match='退出码 3'):
        deployer._run([sys.executable, '-c', "print('npm ERR! missing script');import sys;sys.exit(3)"], job=j)
    assert 'npm ERR! missing script' in message(j)


def test_long_output_is_capped(client, monkeypatch):
    j = job()
    monkeypatch.setattr(deployer, 'LOG_LIMIT', 1000)
    deployer._run([sys.executable, '-c', "print('x'*5000)"], job=j)
    assert len(message(j)) == 1000


def test_command_that_never_ends_is_stopped(client):
    j = job()
    with pytest.raises(deployer.DeploymentError, match='没有结束'):
        deployer._run([sys.executable, '-c', "import time;print('started',flush=True);time.sleep(30)"], job=j, timeout=1)
    assert 'started' in message(j)


def test_restart_keeps_the_running_version(client, tmp_path):
    r = client.post('/api/projects', json={'account_id': 1, 'repository': 'me/app', 'deploy_root': str(tmp_path/'app'), 'start_command': 'echo up'})
    pid = r.json()['id']
    p = client.get(f'/api/projects/{pid}').json()
    root = Path(p['deploy_root']); release = root/'releases'/'5'; release.mkdir(parents=True)
    # Nothing deployed yet: nothing to restart.
    j = job(pid, 'restart')
    deployer._execute(j, {}, p, 'restart')
    with db.connect() as c:
        assert c.execute('SELECT status FROM deployments WHERE id=?', (j,)).fetchone()['status'] == 'failed'
    deployer._pointer(root, release)
    with db.connect() as c:
        c.execute("INSERT INTO deployments(project_id,action,status,commit_sha,release_dir,previous_release,started_at,finished_at) "
                  "VALUES(?,'deploy','succeeded','abc123',?,?,?,?)", (pid, str(release), str(root/'releases'/'4'), db.now(), db.now()))
    j = job(pid, 'restart')
    with patch.object(deployer, '_start') as start:
        deployer._execute(j, {}, p, 'restart')
    assert start.call_args.args[1] == release
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM deployments WHERE id=?', (j,)).fetchone())
    assert row['status'] == 'succeeded' and row['commit_sha'] == 'abc123'
    assert row['previous_release'] == str(root/'releases'/'4')   # rollback still targets the same version
    async def latest(*a): return {'sha': 'abc123', 'message': 'm', 'date': ''}
    with patch.object(api.github, 'latest_commit', latest), patch.object(api.cli_auth, 'token', return_value='t'):
        assert client.get(f'/api/projects/{pid}/check').json()['available'] is False
    with patch.object(deployer.threading.Thread, 'start'):
        assert client.post(f'/api/projects/{pid}/restart').json()['status'] == 'queued'
    deployer._locks[pid].release()


def fake_container(name, status, health='', restarts=0):
    c = MagicMock()
    c.name, c.status = name, status
    c.attrs = {'State': {'StartedAt': '2026-11-18T03:00:00.123456789Z', **({'Health': {'Status': health}} if health else {})}, 'RestartCount': restarts}
    c.logs.return_value = f'2026-11-18T03:00:01Z {name} listening\n'.encode()
    return c


def compose_project(containers):
    client = MagicMock(); client.containers.list.return_value = containers
    return patch('docker.from_env', return_value=client)


def test_status_of_compose_systemd_and_command_projects():
    compose = {'runtime': 'compose', 'runtime_config': json.dumps({'project': 'sub2api', 'files': []}), 'healthcheck_url': ''}
    with compose_project([fake_container('sub2api', 'running', 'healthy'), fake_container('sub2api-redis', 'running')]):
        s = runtimes.status(compose)
    assert s['state'] == 'running' and [i['name'] for i in s['items']] == ['sub2api', 'sub2api-redis']
    with compose_project([fake_container('sub2api', 'running', 'unhealthy'), fake_container('sub2api-redis', 'running')]):
        assert runtimes.status(compose)['state'] == 'degraded'
    with compose_project([fake_container('sub2api', 'exited')]):
        assert runtimes.status(compose)['state'] == 'stopped'
    with compose_project([]):
        assert runtimes.status(compose)['detail'] == '没有找到这个 Compose 项目的容器'
    with patch('docker.from_env', side_effect=Exception('no docker')):
        assert runtimes.status(compose)['state'] == 'unknown'

    unit = {'runtime': 'systemd', 'runtime_config': json.dumps({'unit': 'qiyu-diary.service'}), 'healthcheck_url': 'http://x/health'}
    shown = Mock(returncode=0, stdout='ActiveState=active\nSubState=running\nActiveEnterTimestamp=@1795000000\nNRestarts=2\n')
    with patch.object(runtimes.subprocess, 'run', return_value=shown), patch.object(runtimes, '_healthy', return_value=False):
        s = runtimes.status(unit)
    assert s['state'] == 'degraded' and s['health'] is False and s['items'][0]['restarts'] == 2
    assert s['items'][0]['since'].startswith('2026-11-18T')
    # Older systemd without --timestamp=unix: retried without it, the text kept as is.
    old = [Mock(returncode=1, stdout=''), Mock(returncode=0, stdout='ActiveState=failed\nActiveEnterTimestamp=Tue 2026-11-18 10:16:56 CST\n')]
    with patch.object(runtimes.subprocess, 'run', side_effect=old), patch.object(runtimes, '_healthy', return_value=False):
        s = runtimes.status(unit)
    assert s['state'] == 'stopped' and s['items'][0]['since'] == 'Tue 2026-11-18 10:16:56 CST'

    command = {'runtime': 'command', 'runtime_config': '{}', 'healthcheck_url': ''}
    assert runtimes.status(command)['state'] == 'unknown'
    with patch.object(runtimes, '_healthy', return_value=True):
        assert runtimes.status({**command, 'healthcheck_url': 'http://x'})['state'] == 'running'


def test_service_logs_api(client, tmp_path):
    r = client.post('/api/projects', json={'account_id': 1, 'repository': 'me/app', 'deploy_root': str(tmp_path/'a'), 'runtime': 'compose'})
    pid = r.json()['id']
    with compose_project([fake_container('app-web-1', 'running'), fake_container('app-db-1', 'running')]):
        d = client.get(f'/api/projects/{pid}/logs?lines=50').json()
        assert '== app-db-1 ==' in d['text'] and 'app-web-1 listening' in d['text']
        assert client.get(f'/api/projects/{pid}/status').json()['state'] == 'running'
        assert client.get('/api/projects/status').json()[str(pid)]['state'] == 'running'
    r = client.post('/api/projects', json={'account_id': 1, 'repository': 'me/b', 'deploy_root': str(tmp_path/'b'), 'start_command': 'pm2 restart b'})
    assert client.get(f"/api/projects/{r.json()['id']}/logs").status_code == 400
    assert client.get(f'/api/projects/{pid}/logs?lines=5').status_code == 422
