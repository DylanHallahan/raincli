"""Sign-in limits (protocol §18.1 items 1 and 2), real PostgreSQL: the limiter counts concurrent attempts
exactly, successes never accumulate, and a recognised device passes only the per-email ceiling."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from api_helpers import auth
from raincli_server import identity
from raincli_server.web import auth as web_auth
from test_app_login import PASSWORD, WRONG, app_login
from test_web_app import csrf_of, login

NEW_PASSWORD = "a new password 123"


def fill_email_ceiling(limiter, email="alice@example.test"):
    for i in range(limiter.per_email):
        limiter.failure(f"198.51.100.{i % 250}", email)


def relogin(client, **kw):
    """Sign in again in the same browser: a new session, the same device cookie."""
    client.cookies.delete(web_auth.SESSION_COOKIE)
    return login(client, **kw)


def device_cookies(response):
    return [c for c in response.headers.get_list("set-cookie") if c.startswith(web_auth.KNOWN_DEVICE_COOKIE + "=")]


def change_password(browser, current=PASSWORD, new=NEW_PASSWORD):
    page = browser.get("/app/account").text
    return browser.post("/app/account/password", data={"csrf_token": csrf_of(page), "current_password": current,
                                                        "password": new, "password_confirm": new},
                        follow_redirects=False)


# The limiter ---------------------------------------------------------------------------------

def test_concurrent_attempts_are_admitted_no_further_than_the_limit():
    limiter = web_auth.LoginLimiter(per_pair=6, per_ip=30, per_email=50)
    start = threading.Barrier(40)
    admitted = []

    def one():
        start.wait()
        admitted.append(limiter.attempt("192.0.2.1", "alice@example.test"))
    threads = [threading.Thread(target=one) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([a for a in admitted if a is not None]) == 6
    assert limiter.blocked("192.0.2.1", "alice@example.test")


def test_concurrent_successes_remove_only_their_own_entries():
    limiter = web_auth.LoginLimiter(per_pair=100, per_ip=100, per_email=100)
    attempts = [limiter.attempt("192.0.2.1", f"user{i % 4}@example.test") for i in range(40)]
    winners, losers = attempts[::2], attempts[1::2]
    start = threading.Barrier(len(winners))

    def win(a):
        start.wait()
        limiter.success(a)
    threads = [threading.Thread(target=win, args=(a,)) for a in winners]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    remaining = list(limiter._hits["ip:192.0.2.1"])
    assert remaining == [a.entry for a in losers if a.entry in remaining] and len(remaining) <= len(losers)
    assert not any(a.entry in remaining for a in winners)


def test_concurrent_wrong_passwords_respect_limits(client, world, app, monkeypatch):
    """Requests that overlap a slow password check are each counted before it runs."""
    limiter = app.state.web_login_limiter
    real = identity.authenticate_user

    def slow(session, email, password):
        time.sleep(0.4)
        return real(session, email, password)
    monkeypatch.setattr(identity, "authenticate_user", slow)
    start = threading.Barrier(16)
    statuses = []

    def one():
        with TestClient(app, client=("192.0.2.9", 40000)) as c:
            start.wait()
            statuses.append(app_login(c, password=WRONG).status_code)
    threads = [threading.Thread(target=one) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(statuses) == [401] * limiter.per_pair + [429] * (16 - limiter.per_pair)
    with TestClient(app, client=("192.0.2.10", 40000)) as other:
        assert app_login(other).status_code == 201  # the right password elsewhere still works


def test_forty_successful_sign_ins_from_one_ip_are_never_blocked(client, world, app):
    limiter = app.state.web_login_limiter
    for _ in range(40):
        assert relogin(client).status_code == 303
    token = app_login(client).json()["token"]
    for _ in range(40):
        r = app_login(client, previous_token=token)
        assert r.status_code == 200, r.text
        token = r.json()["token"]
    assert not limiter.blocked("testclient", "alice@example.test")
    assert "ip:testclient" not in limiter._hits


def test_a_refusal_after_a_wrong_current_password_stays_counted(client, world, app):
    limiter = app.state.web_login_limiter
    assert login(client).status_code == 303
    for _ in range(limiter.per_pair):
        assert change_password(client, current=WRONG).status_code == 400
    assert change_password(client).status_code == 429


# Recognised devices -------------------------------------------------------------------------------

def test_the_device_cookie_is_strict_and_limited_to_the_sign_in_routes(client, world):
    cookies = device_cookies(login(client))
    assert len(cookies) == 2
    paths = set()
    for cookie in cookies:
        attrs = {part.strip().split("=")[0].lower(): part.strip() for part in cookie.split(";")}
        assert "httponly" in attrs and attrs["samesite"].lower() == "samesite=strict"
        assert attrs["max-age"] == "Max-Age=2592000"
        paths.add(attrs["path"])
    assert paths == {"Path=/login", "Path=/app/account/password"}


def test_a_recognised_browser_passes_the_email_ceiling(client, world, app):
    assert login(client).status_code == 303
    fill_email_ceiling(app.state.web_login_limiter)
    with TestClient(app, client=("203.0.113.8", 40000)) as stranger:
        assert login(stranger).status_code == 429  # unknown devices are unchanged
    assert relogin(client).status_code == 303


def test_a_recognised_device_still_counts_against_the_pair_and_ip_limits(client, world, app):
    assert login(client).status_code == 303
    limiter = app.state.web_login_limiter
    for _ in range(limiter.per_pair):
        relogin(client, password=WRONG)
    assert relogin(client).status_code == 429  # the pair limit applies
    limiter.per_ip = 0
    assert relogin(client, email="bob@example.test").status_code == 429


def test_a_recognised_device_is_ignored_after_ten_failures(client, world, app):
    assert login(client).status_code == 303
    limiter = app.state.web_login_limiter
    token = client.cookies.get(web_auth.KNOWN_DEVICE_COOKIE, path="/login")
    for i in range(web_auth.DEVICE_FAILURES):  # spread over IPs so no pair or IP limit is reached
        with TestClient(app, client=(f"192.0.2.{50 + i}", 40000)) as c:
            assert app_login(c, password=WRONG, known_device=token).status_code == 401
    fill_email_ceiling(limiter)
    assert relogin(client).status_code == 429


def test_another_users_device_token_is_ignored(client, world, app):
    assert login(client, email="bob@example.test").status_code == 303  # this browser knows bob, not alice
    fill_email_ceiling(app.state.web_login_limiter)
    assert relogin(client).status_code == 429
    bob = app_login(client, email="bob@example.test", machine_name="bob-pc").json()["known_device"]
    assert app_login(client, machine_name="alice-two", known_device=bob).status_code == 429


def test_a_password_change_revokes_tokens_and_reissues_one_to_the_changing_device(client, world, app):
    app_token = app_login(client).json()["known_device"]
    assert login(client).status_code == 303
    old_cookie = client.cookies.get(web_auth.KNOWN_DEVICE_COOKIE, path="/login")
    with TestClient(app, client=("203.0.113.20", 40000)) as changer:
        assert login(changer).status_code == 303
        r = change_password(changer)
        assert r.status_code == 303 and len(device_cookies(r)) == 2
        fill_email_ceiling(app.state.web_login_limiter)
        assert relogin(changer, password=NEW_PASSWORD).status_code == 303  # its new token works
    assert relogin(client, password=NEW_PASSWORD).status_code == 429  # every other device's token is revoked
    assert client.cookies.get(web_auth.KNOWN_DEVICE_COOKIE, path="/login") == old_cookie
    assert app_login(client, password=NEW_PASSWORD, known_device=app_token).status_code == 429


def test_the_app_keeps_its_token_across_sign_out_and_a_fresh_install_has_none(client, world, app):
    first = app_login(client)
    token, machine = first.json()["known_device"], first.json()["token"]
    assert web_auth.KNOWN_DEVICE_RE.fullmatch(token)
    assert client.post("/api/v1/app/sign-out", headers=auth(machine)).status_code == 200
    fill_email_ceiling(app.state.web_login_limiter)
    assert app_login(client, machine_name="alice-two").status_code == 429  # a fresh install
    again = app_login(client, machine_name="alice-two", known_device=token)
    assert again.status_code == 201 and again.json()["known_device"] != token
    assert app_login(client, machine_name="alice-three", known_device="rkd1_x").status_code == 429
    assert app_login(client, machine_name="alice-three", known_device=7).status_code == 400


def test_device_tokens_expire_and_are_signed_for_one_user_and_epoch(world, settings, session):
    alice = session.get(type(world["users"]["alice"]), world["users"]["alice"].id)
    token = web_auth.issue_known_device(settings, alice)
    assert web_auth.known_device_id(settings, token, alice)
    head, device, issued, epoch, sig = token.split(".")
    old = int(time.time() - web_auth.KNOWN_DEVICE_TTL.total_seconds() - 1)
    assert not web_auth.known_device_id(settings, f"{head}.{device}.{old}.{epoch}.{sig}", alice)
    assert not web_auth.known_device_id(settings, f"{head}.{device}.{issued}.{epoch}.{'0' * 64}", alice)
    assert not web_auth.known_device_id(settings, token + "\n", alice)
    assert not web_auth.known_device_id(settings, token, world["users"]["bob"])
    identity.bump_credential_epoch(alice)
    assert not web_auth.known_device_id(settings, token, alice)
    identity.set_user_active(session, alice, True)
    fresh = web_auth.issue_known_device(settings, alice)
    identity.set_user_active(session, alice, False)
    assert not web_auth.known_device_id(settings, fresh, alice)  # a disable ends it too
