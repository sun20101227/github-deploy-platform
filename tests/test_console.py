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
    # The unauthenticated OAuth routes are gone; accounts connect only through gh behind the admin key.
    assert client.get('/auth/github/start',headers={'Authorization':''},follow_redirects=False).status_code==404
    assert client.get('/auth/github/callback?code=x&state=y',headers={'Authorization':''}).status_code==404


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


def test_interrupted_jobs_are_closed_on_startup(client,tmp_path):
    p=project(client,tmp_path)
    with db.connect() as c:
        for status in ('queued','running','succeeded'):
            c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES(1,'deploy',?,?)",(status,db.now()))
    assert client.put('/api/projects/1',json=p).status_code==409
    assert db.recover_interrupted()==2
    with db.connect() as c:
        rows=c.execute('SELECT status,message,finished_at FROM deployments ORDER BY id').fetchall()
    assert [r['status'] for r in rows]==['failed','failed','succeeded']
    assert '重启' in rows[0]['message'] and rows[0]['finished_at']
    assert rows[2]['message']==''
    assert client.put('/api/projects/1',json=p).status_code==200


def test_push_during_deploy_runs_once_more_afterwards(client,tmp_path):
    import hashlib, hmac, json
    p=project(client,tmp_path)
    body=json.dumps({'ref':'refs/heads/main','repository':{'full_name':'test/repo'}}).encode()
    headers={'x-github-event':'push','content-type':'application/json',
             'x-hub-signature-256':'sha256='+hmac.new(p['webhook_secret'].encode(),body,hashlib.sha256).hexdigest()}
    def jobs():
        with db.connect() as c:
            return [r['action'] for r in c.execute('SELECT action FROM deployments ORDER BY id')]
    with patch.object(deployer.threading.Thread,'start'):
        try:
            deployer.deploy(p['id'],account={},project=p)
            lock=deployer._locks[p['id']]
            for _ in range(2):
                assert client.post('/webhooks/github/1',content=body,headers=headers).json()=={'ok':True,'queued':True}
            assert jobs()==['deploy']
            deployer._finish(p['id'],lock)
            assert jobs()==['deploy','webhook']
            assert lock.locked()
            deployer._finish(p['id'],lock)
            assert jobs()==['deploy','webhook']
            assert not lock.locked()
        finally:
            deployer._pending.discard(p['id'])
            if deployer._locks[p['id']].locked():
                deployer._locks[p['id']].release()


def test_old_releases_are_pruned_but_rollback_target_stays(client,tmp_path):
    import os, stat
    p=project(client,tmp_path)
    root=Path(p['deploy_root']);releases=root/'releases'
    for name in ['1','2','3','4','5','6','7','8','manual']:
        (releases/name).mkdir(parents=True)
    locked=releases/'2'/'object';locked.write_text('x');os.chmod(locked,stat.S_IREAD)
    assert deployer._prune(root,[releases/'8',str(releases/'1'),''])==2
    assert sorted(d.name for d in releases.iterdir())==['1','4','5','6','7','8','manual']


def test_successful_deploy_prunes_and_stays_successful(client,tmp_path):
    p=project(client,tmp_path)
    root=Path(p['deploy_root'])
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES(1,'deploy','queued',?)",(db.now(),)).lastrowid
    lock=threading.Lock();lock.acquire()
    with patch.object(deployer,'_run',return_value='c'*40),patch.object(deployer,'_start'),patch.object(deployer,'_prune',side_effect=OSError('busy')) as prune:
        deployer._worker(job,{'access_token':'fake'},p,'deploy',lock)
    assert [Path(x) for x in prune.call_args.args[1][:1]]==[root/'releases'/str(job)]
    with db.connect() as c:
        row=c.execute('SELECT status,message FROM deployments WHERE id=?',(job,)).fetchone()
    assert row['status']=='succeeded'
    assert '清理未完成' in row['message']
    assert not lock.locked()


def test_new_project_restarts_through_compose_without_a_command(client,tmp_path):
    import json
    from deploy_platform import runtimes
    base={'account_id':1,'repository':'test/My.App','deploy_root':str(tmp_path/'compose-app'),'runtime':'compose'}
    for bad in ({'runtime':'systemd','runtime_config':{'unit':'x.service'}},
                {'runtime_config':{'files':['../outside.yaml']}},
                {'runtime_config':{'files':[str(tmp_path/'abs.yaml')]}},
                {'runtime_config':{'project':'Bad Name'}},
                {'runtime':'command'}):
        assert client.post('/api/projects',json={**base,**bad}).status_code in (400,409)
    r=client.post('/api/projects',json=base)
    assert r.status_code==200,r.text
    p=client.get(f"/api/projects/{r.json()['id']}").json()
    assert p['runtime']=='compose' and p['start_command']==''
    assert json.loads(p['runtime_config'])=={'project':'my-app','files':[]}
    # Each release is a fresh clone: the compose file is found there, the shared .env beside releases/.
    root=Path(p['deploy_root']);release=root/'releases'/'7';release.mkdir(parents=True)
    with patch.object(runtimes,'DockerClient') as docker:
        with pytest.raises(RuntimeError,match='没有找到 compose.yaml'):
            runtimes.restart(p,release)
        (release/'docker-compose.yml').write_text('services: {}')
        (root/'.env').write_text('TOKEN=1')
        assert runtimes.restart(p,release)
        options=docker.call_args.kwargs
        assert options['compose_project_name']=='my-app'
        assert options['compose_files']==[str((release/'docker-compose.yml').resolve())]
        assert options['compose_env_files']==[str(root/'.env')]
        docker.return_value.compose.up.assert_called_once()
    # The settings page can switch an existing release project between the two ways.
    assert client.put(f"/api/projects/{p['id']}",json={**p,'runtime':'command','start_command':'pm2 restart app'}).status_code==200
    assert client.put(f"/api/projects/{p['id']}",json={**p,'runtime_config':{'project':'my-app','files':['deploy/compose.yaml']}}).status_code==200
    assert json.loads(client.get(f"/api/projects/{p['id']}").json()['runtime_config'])['files']==['deploy/compose.yaml']


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
    import itertools
    project={'deploy_root':'.','start_command':'echo ok','healthcheck_url':'http://localhost/health'}
    response=lambda code: type('Response',(),{'status_code':code})()
    def start(project,answers,clock):
        with patch.object(deployer,'_run'),patch.object(deployer.httpx,'get',side_effect=answers) as get,\
             patch.object(deployer.time,'sleep'),patch.object(deployer.time,'monotonic',side_effect=clock):
            deployer._start(project,Path('.'))
        return get.call_count
    # One clock reading per attempt, 10 seconds apart: the default 45 seconds allows 5 more tries.
    with pytest.raises(deployer.DeploymentError,match='健康检查未通过（已等待 45 秒）'):
        start(project,itertools.repeat(response(503)),itertools.count(0,10))
    # A slow service passes once the project allows a longer wait.
    slow=[response(503)]*8+[response(200)]
    with pytest.raises(deployer.DeploymentError):
        start(project,list(slow),itertools.count(0,10))
    assert start({**project,'healthcheck_timeout':120},list(slow),itertools.count(0,10))==9


def test_healthcheck_timeout_is_saved_and_validated(client,tmp_path):
    p=project(client,tmp_path)
    assert p['healthcheck_timeout']==45
    assert client.put('/api/projects/1',json={**p,'healthcheck_timeout':'180'}).status_code==200
    assert client.get('/api/projects/1').json()['healthcheck_timeout']==180
    for bad in (0,601,'soon'):
        assert client.put('/api/projects/1',json={**p,'healthcheck_timeout':bad}).status_code==422


def test_repository_list_follows_pages_only_inside_github():
    import asyncio, httpx
    from deploy_platform import github
    seen=[]
    def handler(request):
        seen.append(str(request.url))
        page=int(request.url.params.get('page','1'))
        repos=[{'full_name':f'me/repo{page}','private':False,'default_branch':None}]
        nxt={1:'https://api.github.com/user/repos?per_page=100&page=2',2:'https://evil.example/user/repos?page=3'}.get(page)
        return httpx.Response(200,json=repos,headers={'Link':f'<{nxt}>; rel="next"'} if nxt else {})
    real=httpx.AsyncClient
    with patch.object(github.httpx,'AsyncClient',lambda **kw: real(transport=httpx.MockTransport(handler),**kw)):
        result=asyncio.run(github.repositories('token'))
    assert [r['full_name'] for r in result]==['me/repo1','me/repo2']
    assert result[0]['default_branch']=='main'
    assert len(seen)==2 and all(u.startswith('https://api.github.com/') for u in seen)
