"""Password changes, legacy upgrades, session invalidation and API cache safety."""
import hashlib
import re
import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient
from raincli_server import identity, security
from raincli_server.models import User, WebSession
OLD = 'correct horse battery'
NEW = 'a new unique test password'

def csrf(r):
    return re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)

def login(c, password=OLD):
    return c.post('/login', data={'email':'alice@example.test','password':password,
        'csrf_token':csrf(c.get('/login'))}, follow_redirects=False)

def change(c, current=OLD, new=NEW, confirm=None):
    return c.post('/app/account/password',data={'csrf_token':csrf(c.get('/app/account')),
        'current_password':current,'password':new,'password_confirm':new if confirm is None else confirm},
        follow_redirects=False)

def test_change_password_revokes_other_sessions_preserves_agents(client, world, session, app):
    assert login(client).status_code == 303
    old_cookie = client.cookies.get('raincli_session')
    old_csrf = csrf(client.get('/app/account'))
    with TestClient(app) as other:
        assert login(other).status_code == 303
        assert change(client).status_code == 303
        assert client.cookies.get('raincli_session') != old_cookie
        assert client.get('/app/account').status_code == 200
        assert other.get('/app/account',follow_redirects=False).status_code == 303
        other.cookies.clear()
        other.cookies.set('raincli_session',old_cookie)
        assert other.post('/app/account/password',data={'csrf_token':old_csrf,
            'current_password':NEW,'password':OLD,'password_confirm':OLD}).status_code == 403
    session.expire_all()
    user=session.get(User,world['users']['alice'].id)
    assert security.verify_password(NEW,user.password_hash)
    assert not security.verify_password(OLD,user.password_hash)
    assert identity.authenticate_agent(session,world['tokens']['alice'],touch=False)
    sessions=session.scalars(select(WebSession).where(WebSession.user_id==user.id)).all()
    assert sum(w.revoked_at is None for w in sessions)==1
    client.cookies.clear()
    assert login(client,OLD).status_code==400
    assert login(client,NEW).status_code==303

def test_change_rejects_csrf_wrong_current_and_invalid_replacements(client,world,session):
    login(client)
    initial=session.get(User,world['users']['alice'].id).password_hash
    assert client.post('/app/account/password',data={'current_password':OLD,
        'password':NEW,'password_confirm':NEW}).status_code==403
    for kwargs in [{'current':'not the old password'},{'new':'short'},{'confirm':'mismatch'},{'new':OLD}]:
        r=change(client,**kwargs)
        assert r.status_code==400
        assert NEW not in r.text and OLD not in r.text
    session.expire_all()
    assert session.get(User,world['users']['alice'].id).password_hash==initial
    assert client.get('/app/account').status_code==200

def test_password_change_attempts_are_rate_limited(client,world):
    login(client)
    for _ in range(6): assert change(client,current='wrong password').status_code==400
    assert change(client).status_code==429

def test_account_requires_login(client,world):
    assert client.get('/app/account',follow_redirects=False).headers['location']=='/login?next=/app/account'
    assert client.post('/app/account/password').status_code==403

def test_legacy_hash_upgraded_only_on_successful_login(client,world,session):
    salt=bytes(range(16))
    digest=hashlib.scrypt(OLD.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
    legacy=f'scrypt$16384$8$1${salt.hex()}${digest.hex()}'
    user=world['users']['alice'];user.password_hash=legacy;session.commit()
    assert login(client,'wrong password').status_code==400
    session.refresh(user);assert user.password_hash==legacy
    assert login(client).status_code==303
    session.refresh(user)
    assert user.password_hash.startswith('scrypt$16384$8$5$')
    assert security.verify_password(OLD,user.password_hash)
    assert not security.password_needs_upgrade(user.password_hash)

def test_overlong_login_is_not_truncated(client,world,session):
    password='x'*256
    world['users']['alice'].password_hash=security.hash_password(password);session.commit()
    assert login(client,password+'extra').status_code==400
    assert login(client,password).status_code==303

def test_revoked_session_cannot_change_password_via_stale_viewer(client,world,session):
    login(client)
    ws=session.scalar(select(WebSession).where(WebSession.user_id==world['users']['alice'].id))
    ws.revoked_at=identity.now();session.commit()
    with pytest.raises(identity.PermissionDenied):
        identity.change_password(session,world['users']['alice'].id,ws.id,OLD,NEW)

def test_api_success_error_and_long_poll_responses_are_not_cached(client,world):
    headers={'Authorization':'Bearer '+world['tokens']['alice']}
    for path,auth,expected in [('/api/v1/agents',{},401),('/api/v1/agents',headers,200),
        ('/api/v1/inbox?wait=0',headers,200),('/api/v1/not-real',headers,404)]:
        r=client.get(path,headers=auth)
        assert r.status_code==expected
        assert r.headers['cache-control']=='no-store'


def test_password_change_serializes_with_old_password_login(client, world, session, engine):
    """A login that waits behind a password change must see the replacement hash."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from raincli_server.db import make_sessionmaker
    login(client)
    user_id = world['users']['alice'].id
    ws = session.scalar(select(WebSession).where(WebSession.user_id == user_id))
    factory = make_sessionmaker(engine)
    started = Event()

    def old_login():
        with factory() as pending:
            started.set()
            user = identity.authenticate_user(pending, 'alice@example.test', OLD)
            pending.commit()
            return user is not None

    with factory() as changing:
        identity.change_password(changing, user_id, ws.id, OLD, NEW)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(old_login)
            assert started.wait(5)
            # Commit while the login waits on our row lock. Its SELECT must refresh
            # the hash and reject the old password instead of issuing a new session.
            changing.commit()
            assert result.result(timeout=10) is False
