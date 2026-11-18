import pytest
from fastapi.testclient import TestClient

from deploy_platform import api, auth, config, db

PASSWORD = 'correct horse battery'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATABASE', tmp_path/'auth.db')
    monkeypatch.setenv('DEPLOY_ADMIN_TOKEN', 'old-token')
    monkeypatch.setattr(auth, 'throttle', auth.Throttle())
    db.init(); auth.init()
    with TestClient(api.app, headers={'X-Deploy-Request': '1'}) as c:
        yield c


def setup_admin(client, username='admin', password=PASSWORD, token='old-token'):
    return client.post('/api/auth/setup', json={'username': username, 'password': password, 'token': token})


def test_first_admin_needs_the_old_management_key(client):
    state = client.get('/api/auth/state').json()
    assert state == {'admin_exists': False, 'logged_in': False, 'username': '', 'setup_needs_token': True, 'setup_allowed': True}
    assert client.get('/api/projects').status_code == 401
    assert setup_admin(client, token='wrong').status_code == 401
    assert setup_admin(client, password='short').status_code == 400
    assert setup_admin(client, username='a b').status_code == 400
    r = setup_admin(client)
    assert r.status_code == 200, r.text
    cookie = r.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie
    assert client.get('/api/auth/state').json()['username'] == 'admin'
    assert client.get('/api/projects').status_code == 200
    # Only once: a second setup cannot replace the admin.
    assert setup_admin(client, username='intruder').status_code == 409


def test_login_logout_and_wrong_password(client):
    setup_admin(client)
    client.post('/api/auth/logout')
    assert client.get('/api/projects').status_code == 401
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong password'}).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'nobody', 'password': PASSWORD}).status_code == 401
    r = client.post('/api/auth/login', json={'username': 'ADMIN', 'password': PASSWORD})
    assert r.status_code == 200 and r.json()['username'] == 'admin'
    assert client.get('/api/projects').status_code == 200
    # The session is stored hashed; the cookie value itself is not in the database.
    token = client.cookies.get(auth.COOKIE)
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM admin_sessions WHERE token_hash=?', (token,)).fetchone()
    client.post('/api/auth/logout')
    client.cookies.set(auth.COOKIE, token)   # an old cookie no longer works after logout
    assert client.get('/api/projects').status_code == 401


def test_cookie_requests_that_change_things_need_the_header(client):
    setup_admin(client)
    assert client.post('/api/github/connect', headers={'X-Deploy-Request': ''}).status_code == 403
    # Reading is fine without it, and the Bearer token for scripts does not need it either.
    assert client.get('/api/projects', headers={'X-Deploy-Request': ''}).status_code == 200
    with TestClient(api.app) as script:
        assert script.get('/api/projects', headers={'Authorization': 'Bearer old-token'}).status_code == 200
        assert script.get('/api/projects', headers={'Authorization': 'Bearer nope'}).status_code == 401


def test_repeated_failures_lock_the_login(client):
    setup_admin(client)
    client.post('/api/auth/logout')
    for _ in range(auth.Throttle.PER_CLIENT):
        assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'wrong password'}).status_code == 401
    r = client.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD})
    assert r.status_code == 429 and '分钟后再试' in r.json()['detail']


def test_password_change_signs_out_other_browsers(client):
    setup_admin(client)
    other = TestClient(api.app, headers={'X-Deploy-Request': '1'})
    assert other.post('/api/auth/login', json={'username': 'admin', 'password': PASSWORD}).status_code == 200
    assert client.post('/api/auth/password', json={'current': 'wrong password', 'new': 'a new long password'}).status_code == 403
    assert client.post('/api/auth/password', json={'current': PASSWORD, 'new': 'short'}).status_code == 400
    assert client.post('/api/auth/password', json={'current': PASSWORD, 'new': 'a new long password'}).status_code == 200
    assert client.get('/api/projects').status_code == 200      # this browser stays signed in
    assert other.get('/api/projects').status_code == 401       # the other one is signed out
    assert other.post('/api/auth/login', json={'username': 'admin', 'password': 'a new long password'}).status_code == 200


def test_without_any_key_only_this_machine_can_create_the_admin(client, monkeypatch):
    monkeypatch.delenv('DEPLOY_ADMIN_TOKEN')
    # The test client counts as this machine; a request through the proxy does not.
    assert client.post('/api/auth/setup', json={'username': 'admin', 'password': PASSWORD},
                       headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 403
    assert client.get('/api/projects', headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 401
    assert client.post('/api/auth/setup', json={'username': 'admin', 'password': PASSWORD}).status_code == 200


def test_cli_resets_a_forgotten_password(client, monkeypatch):
    from deploy_platform import __main__ as cli
    setup_admin(client)
    answers = iter(['brand new password', 'brand new password'])
    monkeypatch.setattr('getpass.getpass', lambda prompt='': next(answers))
    assert cli.admin_command(['admin']) == 0
    assert client.get('/api/projects').status_code == 401   # old sessions ended
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'brand new password'}).status_code == 200
    assert cli.admin_command([]) == 2


def test_password_hashes_are_salted_scrypt():
    a, b = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert a != b and a.startswith('scrypt$')
    assert auth.verify_password(PASSWORD, a) and not auth.verify_password('other', a)
    assert not auth.verify_password(PASSWORD, 'garbage')
