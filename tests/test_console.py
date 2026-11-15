import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from deploy_platform import api, config, db, deployer


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATABASE', tmp_path/'test.db')
    monkeypatch.setenv('DEPLOY_ADMIN_TOKEN','test-secret')
    db.init()
    with db.connect() as c:
        c.execute("INSERT INTO github_accounts VALUES(1,'1','tester','fake',?,?)",(db.now(),db.now()))
    with TestClient(api.app, headers={'Authorization':'Bearer test-secret'}) as client:
        yield client


def project(client,tmp_path):
    r=client.post('/api/projects',json={'account_id':1,'repository':'test/repo','deploy_root':str(tmp_path/'app'),'start_command':'echo start'})
    assert r.status_code==200,r.text
    return client.get('/api/projects/'+str(r.json()['id'])).json()


def test_create_edit_and_dispatch(client,tmp_path):
    p=project(client,tmp_path)
    assert client.put('/api/projects/1',json={**p,'branch':'release/v1'}).status_code==200
    with patch.object(api,'deploy',return_value=4) as dispatch:
        assert client.post('/api/projects/1/deploy').json()['deployment_id']==4
        assert dispatch.call_args.kwargs['project']['branch']=='release/v1'
    assert client.get('/api/projects/99').status_code==404


def test_management_auth_and_assets(client):
    for url in ['/api/accounts','/api/projects','/api/self-update']:
        assert client.get(url,headers={'Authorization':''}).status_code==401
    assert client.get('/').status_code==200
    assert client.get('/app.js').status_code==200
    assert client.post('/webhooks/github/1',json={}).status_code==404


def test_cli_connect_never_stores_token(client):
    with patch.object(api.cli_auth,'identity',return_value={'github_id':'9','login':'cli-user'}):
        assert client.post('/api/github/connect').status_code==200
    with db.connect() as c:
        row=c.execute("SELECT access_token FROM github_accounts WHERE github_id='9'").fetchone()
    assert row['access_token']=='gh-cli:9'


def test_failed_activation_restarts_previous(client,tmp_path):
    p=project(client,tmp_path)
    root=Path(p['deploy_root']);old=root/'releases'/'old';old.mkdir(parents=True)
    deployer._pointer(root,old)
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES(1,'deploy','queued',?)",(db.now(),)).lastrowid
    lock=threading.Lock();lock.acquire()
    with patch.object(deployer,'_run',return_value='a'*40),patch.object(deployer,'_start',side_effect=[RuntimeError('new service failed'),None]) as start:
        deployer._worker(job,{'access_token':'fake'},p,'deploy',lock)
    assert deployer._current_release(root)==str(old)
    assert start.call_count==2
    assert start.call_args.args[1]==old
    with db.connect() as c:
        assert c.execute('SELECT status FROM deployments WHERE id=?',(job,)).fetchone()['status']=='rolled_back'
    assert not lock.locked()


def test_duplicate_deploy_rejected(client,tmp_path):
    p=project(client,tmp_path)
    with patch.object(deployer.threading.Thread,'start'):
        deployer.deploy(p['id'],account={},project=p)
        try:
            with pytest.raises(deployer.DeploymentError,match='正在进行'):
                deployer.deploy(p['id'],account={},project=p)
        finally:
            deployer._locks[p['id']].release()


def test_invalid_root_and_duplicate_directory(client,tmp_path):
    p=project(client,tmp_path)
    assert client.post('/api/projects',json=p).status_code==409
    assert client.post('/api/projects',json={**p,'deploy_root':'relative/path'}).status_code==422
    assert client.post('/api/projects',json={**p,'branch':'--upload-pack=bad'}).status_code==422


def test_manual_rollback_starts_target_and_records_commit(client,tmp_path):
    p=project(client,tmp_path)
    root=Path(p['deploy_root']);old=root/'releases'/'old';new=root/'releases'/'new'
    old.mkdir(parents=True);new.mkdir()
    deployer._pointer(root,new)
    with db.connect() as c:
        c.execute("INSERT INTO deployments(project_id,action,status,started_at,release_dir,previous_release) VALUES(1,'deploy','succeeded',?,?,?)",(db.now(),str(new),str(old)))
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES(1,'rollback','queued',?)",(db.now(),)).lastrowid
    lock=threading.Lock();lock.acquire()
    with patch.object(deployer,'_run',return_value='b'*40),patch.object(deployer,'_start') as start:
        deployer._worker(job,{},p,'rollback',lock)
    assert start.call_args.args[1]==old
    assert deployer._current_release(root)==str(old)
    with db.connect() as c:
        row=c.execute('SELECT status,commit_sha FROM deployments WHERE id=?',(job,)).fetchone()
    assert row['status']=='succeeded'
    assert row['commit_sha']=='b'*40


def test_healthcheck_failure_is_not_success():
    with patch.object(deployer,'_run'),patch.object(deployer.httpx,'get',return_value=type('Response',(),{'status_code':503})()),patch.object(deployer.time,'sleep'):
        with pytest.raises(deployer.DeploymentError,match='健康检查'):
            deployer._start({'deploy_root':'.','start_command':'echo ok','healthcheck_url':'http://localhost/health'},Path('.'))
