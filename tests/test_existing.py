import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

from git import Repo
from fastapi.testclient import TestClient
import pytest
from deploy_platform import api, config, db, discovery, existing, runtimes, restart, deployer
from deploy_platform.__main__ import supervise


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'DATABASE',tmp_path/'db.sqlite')
    monkeypatch.setenv('DEPLOY_ADMIN_TOKEN','test')
    db.init()
    with db.connect() as c:
        c.execute("INSERT INTO github_accounts VALUES(1,'1','test','fake',?,?)",(db.now(),db.now()))
    root=tmp_path/'apps'/'demo'
    repo=Repo.init(root,initial_branch='main')
    with repo.config_writer() as w:
        w.set_value('user','name','Test').set_value('user','email','test@example.invalid')
    (root/'app.txt').write_text('old')
    (root/'.gitignore').write_text('.env\n')
    repo.index.add(['app.txt','.gitignore']); repo.index.commit('initial')
    repo.create_remote('origin','https://github.com/example/demo.git')
    (root/'.env').write_text('PRIVATE=preserve-me')
    yield root, repo, TestClient(api.app,headers={'Authorization':'Bearer test'})
    repo.close()


def import_project(root,client):
    r=client.post('/api/projects/import',json={'path':str(root),'account_id':1,'start_command':'echo restart'})
    assert r.status_code==200,r.text
    return client.get('/api/projects/'+str(r.json()['id'])).json()


def test_inventory_and_import_are_read_only(setup):
    root,repo,client=setup;head=repo.head.commit.hexsha
    with patch.object(discovery,'containers',return_value=([],'')),patch.object(discovery,'services',return_value=[]):
        scan=client.post('/api/discovery/scan',json={'root':str(root.parent)}).json()
    assert scan['projects'][0]['repository']=='example/demo'
    assert 'PRIVATE' not in json.dumps(scan)
    p=import_project(root,client)
    assert p['mode']=='existing'
    assert repo.head.commit.hexsha==head
    assert (root/'.env').read_text()=='PRIVATE=preserve-me'
    assert not (root/'current').exists()
    assert client.post('/api/projects/import',json={'path':str(root),'account_id':1,'start_command':'echo restart'}).status_code==409


def test_existing_update_restarts_and_failure_restores(setup):
    root,repo,client=setup
    before=repo.head.commit.hexsha
    p=import_project(root,client)
    (root/'app.txt').write_text('new');repo.index.add(['app.txt']);after=repo.index.commit('new').hexsha
    repo.git.update_index('--refresh')
    repo.git.reset('--keep',before)
    def fake_fetch(*args,**kwargs):
        (Path(repo.git_dir)/'FETCH_HEAD').write_text(after+'\t\tbranch main\n')
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'deploy','queued',?)",(p['id'],db.now())).lastrowid
    start=Mock(side_effect=[RuntimeError('bad health'),None])
    with patch.object(existing.subprocess,'run',side_effect=fake_fetch),patch.object(existing.cli_auth,'token',return_value='fake'):
        existing.execute(job,{'access_token':'fake'},p,'deploy',deployer._note,start,Mock(return_value=''))
    assert repo.head.commit.hexsha==before
    assert start.call_count==2
    assert (root/'.env').read_text()=='PRIVATE=preserve-me'
    with db.connect() as c:
        assert c.execute('SELECT status FROM deployments WHERE id=?',(job,)).fetchone()[0]=='rolled_back'


def test_existing_build_failure_and_output_are_recorded(setup):
    root,repo,client=setup
    before=repo.head.commit.hexsha
    p={**import_project(root,client),'build_command':'make'}
    (root/'app.txt').write_text('new');repo.index.add(['app.txt']);after=repo.index.commit('new').hexsha
    repo.git.update_index('--refresh')
    repo.git.reset('--keep',before)
    def fake_fetch(*args,**kwargs):
        (Path(repo.git_dir)/'FETCH_HEAD').write_text(after+'\t\tbranch main\n')
    def attempt(run,start):
        with db.connect() as c:
            job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'deploy','queued',?)",(p['id'],db.now())).lastrowid
        with patch.object(existing.subprocess,'run',side_effect=fake_fetch),patch.object(existing.cli_auth,'token',return_value='fake'):
            existing.execute(job,{'access_token':'fake'},p,'deploy',deployer._note,start,run)
        with db.connect() as c:
            return c.execute('SELECT status,message FROM deployments WHERE id=?',(job,)).fetchone()
    # The build fails on the new commit, then succeeds again on the restored one.
    row=attempt(Mock(side_effect=[deployer.DeploymentError('npm ERR! missing script: build'),'']),Mock())
    assert row['status']=='rolled_back'
    assert 'npm ERR! missing script: build' in row['message']
    assert repo.head.commit.hexsha==before
    row=attempt(Mock(return_value='built 42 modules'),Mock(side_effect=[deployer.DeploymentError('服务健康检查未通过'),None]))
    assert row['status']=='rolled_back'
    assert 'built 42 modules' in row['message'] and '服务健康检查未通过' in row['message']
    # Git failures may echo credentials, so they keep the generic message.
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'deploy','queued',?)",(p['id'],db.now())).lastrowid
    boom=existing.subprocess.CalledProcessError(128,'git',stderr='Authorization: Basic c2VjcmV0')
    with patch.object(existing.subprocess,'run',side_effect=boom),patch.object(existing.cli_auth,'token',return_value='fake'):
        existing.execute(job,{'access_token':'fake'},p,'deploy',deployer._note,Mock(),Mock(return_value=''))
    with db.connect() as c:
        message=c.execute('SELECT message FROM deployments WHERE id=?',(job,)).fetchone()['message']
    assert 'c2VjcmV0' not in message and '更新或重启失败' in message


def test_dirty_repo_does_not_restart(setup):
    root,repo,client=setup;p=import_project(root,client)
    (root/'app.txt').write_text('local changes')
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'deploy','queued',?)",(p['id'],db.now())).lastrowid
    start=Mock()
    existing.execute(job,{},p,'deploy',deployer._note,start,Mock(return_value=''))
    start.assert_not_called()
    assert (root/'app.txt').read_text()=='local changes'


def test_compose_retains_project_and_waits(tmp_path):
    file=tmp_path/'compose.yaml';file.write_text('services: {}')
    project={'runtime':'compose','runtime_config':json.dumps({'project':'existing-app','files':[str(file)]})}
    with patch.object(runtimes,'DockerClient') as docker:
        assert runtimes.restart(project,tmp_path)
        assert docker.call_args.kwargs['compose_project_name']=='existing-app'
        options=docker.return_value.compose.up.call_args.kwargs
        assert options['force_recreate'] and options['wait']
        assert not options.get('renew_anon_volumes')


def test_supervisor_relaunches_only_on_restart_code():
    first=Mock();first.wait.return_value=75
    second=Mock();second.wait.return_value=0
    with patch('subprocess.Popen',side_effect=[first,second]) as spawn,patch('subprocess.run',return_value=Mock(returncode=0)):
        assert supervise(['test-server'])==0
        assert spawn.call_count==2
        assert spawn.call_args.kwargs['env']['DEPLOY_SUPERVISED']=='1'


def test_self_update_requires_supervisor(setup,monkeypatch):
    _,_,client=setup;monkeypatch.delenv('DEPLOY_SUPERVISED',raising=False)
    with patch.object(api.self_update,'apply') as apply:
        assert client.post('/api/self-update/apply').status_code==409
        apply.assert_not_called()


def test_self_update_schedules_restart(setup,monkeypatch):
    _,_,client=setup;monkeypatch.setenv('DEPLOY_SUPERVISED','1')
    with patch.object(api.self_update,'apply',return_value={'commit':'a'*40}),patch.object(restart,'schedule') as schedule:
        try:
            response=client.post('/api/self-update/apply')
            assert response.status_code==200,response.text
            assert response.json()['restarting']
            schedule.assert_called_once()
        finally:
            deployer.maintenance.clear()


def test_existing_success_and_manual_rollback(setup):
    root,repo,client=setup;p=import_project(root,client)
    before=repo.head.commit.hexsha
    (root/'app.txt').write_text('new version')
    repo.index.add(['app.txt']);after=repo.index.commit('new').hexsha
    repo.git.update_index('--refresh');repo.git.reset('--keep',before)
    def fake_fetch(args,**kwargs):
        assert args[3]=='https://github.com/example/demo.git'
        assert kwargs['timeout']==180
        (Path(repo.git_dir)/'FETCH_HEAD').write_text(after+'\t\tbranch main\n')
    def job(action):
        with db.connect() as c:
            return c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,?,'queued',?)",(p['id'],action,db.now())).lastrowid
    start=Mock()
    with patch.object(existing.subprocess,'run',side_effect=fake_fetch),patch.object(existing.cli_auth,'token',return_value='fake'):
        update=job('update')
        existing.execute(update,{'access_token':'fake'},p,'update',deployer._note,start,Mock(return_value=''))
    assert repo.head.commit.hexsha==after
    start.assert_called_once()
    rollback=job('rollback')
    existing.execute(rollback,{},p,'rollback',deployer._note,start,Mock(return_value=''))
    assert repo.head.commit.hexsha==before
    assert start.call_count==2
    assert (root/'.env').read_text()=='PRIVATE=preserve-me'
    assert repo.remotes.origin.url=='https://github.com/example/demo.git'
    with db.connect() as c:
        assert c.execute('SELECT status FROM deployments WHERE id=?',(rollback,)).fetchone()[0]=='succeeded'


def test_runtime_settings_can_be_edited_and_validate_files(setup):
    root,repo,client=setup;p=import_project(root,client)
    payload={**p,'runtime':'compose','runtime_config':{'project':'original-app','files':['compose.yaml']}}
    assert client.put(f"/api/projects/{p['id']}",json=payload).status_code==409
    (root/'compose.yaml').write_text('services: {}')
    assert client.put(f"/api/projects/{p['id']}",json=payload).status_code==200
    saved=client.get(f"/api/projects/{p['id']}").json()
    assert saved['runtime']=='compose'
    assert json.loads(saved['runtime_config'])['project']=='original-app'


def test_supervisor_relaunches_real_child(tmp_path):
    counter=tmp_path/'counter'
    script=tmp_path/'child.py'
    script.write_text("import pathlib,sys,os\np=pathlib.Path(sys.argv[1])\nn=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(n))\nassert os.environ['DEPLOY_SUPERVISED']=='1'\nsys.exit(75 if n==1 else 0)\n")
    with patch('subprocess.run',return_value=Mock(returncode=0)) as install:
        assert supervise([sys.executable,str(script),str(counter)])==0
    assert counter.read_text()=='2'
    install.assert_called_once()


def test_maintenance_prevents_deploy_and_active_job_prevents_self_update(setup,monkeypatch):
    root,_,client=setup;p=import_project(root,client)
    deployer.maintenance.set()
    try:
        assert client.post(f"/api/projects/{p['id']}/deploy").status_code==409
    finally:
        deployer.maintenance.clear()
    monkeypatch.setenv('DEPLOY_SUPERVISED','1')
    import threading
    lock=threading.Lock();lock.acquire()
    monkeypatch.setattr(deployer,'_locks',{p['id']:lock})
    with patch.object(api.self_update,'apply') as apply:
        assert client.post('/api/self-update/apply').status_code==409
        apply.assert_not_called()


def test_discovery_associates_native_services(setup):
    root,_,_=setup
    units=[{'unit':'demo.service','path':str(root),'status':'active'}]
    with patch.object(discovery,'containers',return_value=([],'')),patch.object(discovery,'services',return_value=units):
        assert discovery.scan(root)['projects'][0]['services']==units
    project={'runtime':'systemd','runtime_config':json.dumps({'unit':'demo.service'})}
    with patch.object(discovery,'service_directory',return_value=str(root)),patch.object(runtimes.subprocess,'run') as run:
        assert runtimes.restart(project,root)
        assert run.call_args_list[0].args[0]==['systemctl','restart','demo.service']
        assert run.call_args_list[1].args[0]==['systemctl','is-active','--quiet','demo.service']


def test_runtime_failures_explain_themselves(setup,tmp_path):
    import subprocess
    from python_on_whales.exceptions import DockerException
    root,_,_=setup
    project={'runtime':'systemd','runtime_config':json.dumps({'unit':'demo.service'})}
    failed=subprocess.CalledProcessError(1,'systemctl',stderr='Job for demo.service failed because the control process exited')
    cases=[([failed],'control process exited'),
           ([subprocess.TimeoutExpired('systemctl',120)],'超过 120 秒'),
           ([Mock(),subprocess.CalledProcessError(3,'systemctl')],'journalctl -u demo.service')]
    for effects,expected in cases:
        with patch.object(discovery,'service_directory',return_value=str(root)),patch.object(runtimes.subprocess,'run',side_effect=effects):
            with pytest.raises(RuntimeError,match=expected):
                runtimes.restart(project,root)
    file=tmp_path/'compose.yaml';file.write_text('services: {}')
    project={'runtime':'compose','runtime_config':json.dumps({'project':'existing-app','files':[str(file)]})}
    for error,expected in [(DockerException(['docker','compose','up'],17),'退出码 17.*服务日志'),
                           (DockerException(['docker','compose','up'],1,stderr=b'port is already allocated'),'port is already allocated')]:
        with patch.object(runtimes,'DockerClient') as docker:
            docker.return_value.compose.up.side_effect=error
            with pytest.raises(RuntimeError,match=expected):
                runtimes.restart(project,tmp_path)


def test_running_services_are_found_without_a_directory(setup,tmp_path):
    root,_,client=setup
    plain=tmp_path/'not-a-repo';plain.mkdir()
    inventory=[{'name':'demo-web-1','status':'running','project':'demo','path':str(root),'files':[str(root/'compose.yaml')]},
               {'name':'other','status':'running','project':'','path':'','files':['']}]
    units=[{'unit':'demo.service','path':str(root),'status':'active'},
           {'unit':'plain.service','path':str(plain),'status':'active'},
           {'unit':'gone.service','path':str(tmp_path/'missing'),'status':'failed'}]
    with patch.object(discovery,'containers',return_value=(inventory,'')),patch.object(discovery,'services',return_value=units):
        found=client.get('/api/discovery/running').json()
        assert [p['path'] for p in found['projects']]==[str(root)]
        item=found['projects'][0]
        assert item['repository']=='example/demo' and not item['imported']
        assert [c['name'] for c in item['containers']]==['demo-web-1']
        assert [u['unit'] for u in item['services']]==['demo.service']
        assert 'PRIVATE' not in json.dumps(found)
        import_project(root,client)
        assert client.get('/api/discovery/running').json()['projects'][0]['imported']
        assert client.post('/api/discovery/scan',json={'root':str(root.parent)}).json()['projects'][0]['imported']


def test_deployment_history_is_limited(setup):
    root,_,client=setup;p=import_project(root,client)
    with db.connect() as c:
        for _ in range(60):
            c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'deploy','failed',?)",(p['id'],db.now()))
    url=f"/api/projects/{p['id']}/deployments"
    ids=[j['id'] for j in client.get(url).json()]
    assert len(ids)==50 and ids==sorted(ids,reverse=True)
    assert len(client.get(url+'?limit=1').json())==1
    assert client.get(url+'?limit=0').status_code==422


def test_remote_cannot_overwrite_ignored_configuration(setup):
    root,repo,client=setup;p=import_project(root,client)
    before=repo.head.commit.hexsha
    # Create the incoming commit in a separate index without replacing the live .env.
    from git import IndexFile, Blob
    import io
    from gitdb import IStream
    incoming=IndexFile.from_tree(repo,before)
    stored=repo.odb.store(IStream('blob',len(b'remote config'),io.BytesIO(b'remote config')))
    incoming.add([Blob(repo,stored.binsha,0o100644,'.env')])
    after=incoming.commit('track env',head=False).hexsha
    def fake_fetch(*args,**kwargs):
        (Path(repo.git_dir)/'FETCH_HEAD').write_text(after+'\t\tbranch main\n')
    with db.connect() as c:
        job=c.execute("INSERT INTO deployments(project_id,action,status,started_at) VALUES (?,'update','queued',?)",(p['id'],db.now())).lastrowid
    start=Mock()
    with patch.object(existing.subprocess,'run',side_effect=fake_fetch),patch.object(existing.cli_auth,'token',return_value='fake'):
        existing.execute(job,{'access_token':'fake'},p,'update',deployer._note,start,Mock(return_value=''))
    assert (root/'.env').read_text()=='PRIVATE=preserve-me'
    assert repo.head.commit.hexsha==before
    start.assert_not_called()
