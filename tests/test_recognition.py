"""Recognition signals, modelled on the real server: 1Panel OpenResty sites, a bare repo the updater
fetches into, an updater unit whose script names the app folder, docker run containers, stopped units."""
import json
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from git import Repo

from deploy_platform import discovery, runtimes


@pytest.fixture
def server(tmp_path, monkeypatch):
    opt = tmp_path/'opt'
    # 1Panel's OpenResty: site config includes /www/sites/<x>/proxy/*.conf, which is /opt/1panel/www on the host
    www = opt/'1panel'/'www'
    (www/'conf.d').mkdir(parents=True)
    for site, domain, port in [('diary', 'diary.qiyulab.org', 3000), ('api', 'api.qiyulab.org', 8080), ('openlist', 'openlist.qiyulab.org', 5244)]:
        (www/'conf.d'/f'{site}.conf').write_text(f'server {{\n listen 443 ssl;\n server_name {domain};\n include /www/sites/{site}/proxy/*.conf;\n}}\n')
        (www/'sites'/site/'proxy').mkdir(parents=True)
        (www/'sites'/site/'proxy'/'root.conf').write_text(f'location / {{\n  proxy_pass http://127.0.0.1:{port};\n}}\n')
    (www/'conf.d'/'static.conf').write_text('server { server_name static.example.org; root /srv/x; }')   # no proxy: ignored
    # The way 1Panel writes some sites: the whole server block on one line.
    (www/'conf.d'/'workbuddy.conf').write_text('server { listen 80; listen 443 ssl; server_name workbuddy.qiyulab.org; root /www/sites/workbuddy/index; include /www/sites/workbuddy/proxy/*.conf; }\n')
    (www/'sites'/'workbuddy'/'proxy').mkdir(parents=True)
    (www/'sites'/'workbuddy'/'proxy'/'root.conf').write_text('location ^~ / { proxy_pass http://127.0.0.1:7863; proxy_set_header Host $host; }\n')
    monkeypatch.setattr(discovery, 'NGINX_DIRS', [str(www/'conf.d')])
    monkeypatch.setattr(discovery, 'CONTAINER_PATHS', {'/www/': str(www) + '/'})
    # systemd unit files
    units = tmp_path/'systemd'; units.mkdir()
    monkeypatch.setattr(discovery, 'SYSTEMD_DIRS', [str(units)])
    # qiyu-diary: releases + current + bare repo with no origin, fetched into by its updater
    diary = opt/'qiyu-diary'
    (diary/'releases'/'20261118-fe99e0d').mkdir(parents=True); (diary/'current').mkdir()
    bare = Repo.init(diary/'repo.git', bare=True, initial_branch='master'); bare.close()
    (diary/'repo.git'/'FETCH_HEAD').write_text("fe99e0d3c1aa\t\tbranch 'main' of github.com:sun20101227/qiyu-diary\n")
    script = tmp_path/'qiyu-update'
    script.write_text('#!/bin/sh\nAPP=' + diary.as_posix() + '\n. /etc/qiyu-updater.conf\n')
    (units/'qiyu-updater.service').write_text(f'[Service]\nType=oneshot\nExecStart={script.as_posix()}\n')
    (units/'qiyu-backup.service').write_text(f'[Service]\nType=oneshot\nExecStart=/usr/bin/env node {diary.as_posix()}/current/backup.mjs\n')
    # qiyu-home: installed but not running, so systemctl does not list it
    home = opt/'qiyu-home'; home.mkdir()
    (home/'package.json').write_text('{"name": "site-creator-vinext-starter", "repository": "https://github.com/cloudflare/vinext"}')
    (units/'qiyu-home.service').write_text(f'[Service]\nWorkingDirectory={home.as_posix()}\nExecStart=/usr/bin/npm start\n')
    # openlist and custom-auth: started with docker run, keeping data in /opt/<app>/data
    for app in ('openlist', 'custom-auth'):
        (opt/app/'data').mkdir(parents=True)
    (opt/'custom-auth'/'Dockerfile').write_text('FROM python:3.12-slim\nCOPY app.py /app.py\n')
    monkeypatch.setattr(discovery, 'APP_PARENTS', discovery.APP_PARENTS | {opt.as_posix()})
    return opt, units


def container(name, image, ports=(), project='', path='', mounts=(), source=''):
    return {'name': name, 'id': name, 'status': 'running', 'project': project, 'path': path, 'files': [''], 'image': image,
            'ports': list(ports), 'app_dir': '' if project else discovery._mount_app_dir(list(mounts)), 'source': source}


def test_sites_map_domains_to_local_ports(server):
    found = {tuple(s['domains']): s['ports'] for s in discovery.sites()}
    assert found == {('diary.qiyulab.org',): [3000], ('api.qiyulab.org',): [8080], ('openlist.qiyulab.org',): [5244],
                     ('workbuddy.qiyulab.org',): [7863]}


def test_listeners_find_the_unit_behind_a_port(monkeypatch):
    ss = Mock(stdout='LISTEN 0 511 127.0.0.1:3000 0.0.0.0:* users:(("next-server (v",pid=11,fd=20))\n'
                     'LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=12,fd=4))\n'
                     'LISTEN 0 4096 *:7863 *:* users:(("wbapi",pid=13,fd=3))\n')
    cgroups = {'/proc/11/cgroup': '0::/system.slice/qiyu-diary.service\n', '/proc/12/cgroup': '0::/system.slice/docker.service\n',
               '/proc/13/cgroup': '0::/system.slice/workbuddy2api.service\n'}
    monkeypatch.setattr(discovery, '_read', lambda p, limit=0: cgroups.get(str(p), ''))
    with patch.object(discovery.subprocess, 'run', return_value=ss):
        # Docker's proxy is left out: container ports come from the container itself.
        assert discovery.listeners() == {3000: 'qiyu-diary.service', 7863: 'workbuddy2api.service'}


def test_the_whole_server_is_recognised(server):
    opt, _ = server
    inventory = [
        container('sub2api', 'weishaw/sub2api:latest', [8080], 'sub2api', (opt/'sub2api').as_posix(), source='Wei-Shaw/sub2api'),
        container('openlist', 'openlistteam/openlist:latest', [5244], mounts=[(opt/'openlist'/'data').as_posix()], source='OpenListTeam/OpenList'),
        container('oplist-api-server', 'qiyulab/openlist-auth:local', [3100], mounts=[(opt/'custom-auth'/'data').as_posix()]),
    ]
    (opt/'sub2api').mkdir(); (opt/'sub2api'/'docker-compose.yml').write_text('services: {}')
    systemctl = Mock(returncode=0, stdout=f'Id=qiyu-diary.service\nWorkingDirectory={(opt/"qiyu-diary"/"current").as_posix()}\nActiveState=active\n')
    with patch.object(discovery, 'containers', return_value=(inventory, '')), \
         patch.object(discovery.subprocess, 'run', return_value=systemctl), \
         patch.object(discovery, 'listeners', return_value={3000: 'qiyu-diary.service'}):
        found = {p['name']: p for p in discovery.running()['projects']}

    diary = found['qiyu-diary']
    # Repository and branch come from where the updater fetches, not from a guess on the name.
    assert (diary['repository'], diary['branch'], diary['repository_from']) == ('sun20101227/qiyu-diary', 'main', 'directory')
    assert diary['updater'] == 'qiyu-updater.service'   # its script names the folder; qiyu-backup is no updater
    assert diary['domains'] == ['diary.qiyulab.org'] and diary['ports'] == [3000]

    sub = found['sub2api']
    assert sub['kind'] == 'image' and sub['domains'] == ['api.qiyulab.org']

    openlist = found['openlist']
    assert openlist['kind'] == 'container' and openlist['repository'] == 'OpenListTeam/OpenList'
    assert openlist['domains'] == ['openlist.qiyulab.org'] and 'docker run' in openlist['note']
    auth = found['custom-auth']
    assert auth['kind'] == 'container' and [c['name'] for c in auth['containers']] == ['oplist-api-server']

    home = found['qiyu-home']
    # A stopped unit still counts; a dependency mentioned in package.json is not taken as the app's repository.
    assert home['services'][0]['status'] == 'inactive' and '没有运行' in home['note']
    assert home['repository'] == ''


def test_mentions_must_be_named_like_the_app():
    texts = ['uses https://github.com/cloudflare/vinext', 'REPO_URL=https://github.com/sun20101227/qiyu-diary.git\n']
    assert discovery._mentioned_repository(texts, ['qiyu-diary']) == 'sun20101227/qiyu-diary'
    assert discovery._mentioned_repository(texts, ['qiyu-home']) == ''


def test_docker_run_container_runtime():
    c = MagicMock(); c.name, c.status = 'openlist', 'running'
    c.attrs = {'State': {'StartedAt': '2026-11-14T11:38:00Z'}, 'RestartCount': 0,
               'NetworkSettings': {'Ports': {'5244/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '5244'}], '5245/tcp': None}}}
    c.logs.return_value = b'2026-11-18T01:00:00Z openlist started\n'
    client = MagicMock(); client.containers.get.return_value = c
    project = {'runtime': 'container', 'runtime_config': json.dumps({'container': 'openlist'}), 'healthcheck_url': ''}
    with patch('docker.from_env', return_value=client), patch.object(discovery, 'sites', return_value=[{'domains': ['openlist.qiyulab.org'], 'ports': [5244]}]):
        s = runtimes.status(project)
        assert s['state'] == 'running' and s['ports'] == [5244] and s['domains'] == ['openlist.qiyulab.org']
        assert 'openlist started' in runtimes.logs(project)['text']
        assert runtimes.validate('container', {'container': 'openlist'}, '/opt/openlist') == {'container': 'openlist'}
        assert runtimes.restart(project, Path('/opt/openlist'))
        c.restart.assert_called_once()
        c.status = 'exited'
        with pytest.raises(RuntimeError, match='重启后状态是 exited'):
            runtimes.restart(project, Path('/opt/openlist'))
    with pytest.raises(RuntimeError, match='容器名'):
        runtimes.validate('container', {'container': '--rm'}, '/opt/x')
