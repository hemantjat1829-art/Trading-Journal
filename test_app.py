import pytest
from fastapi.testclient import TestClient
from app import create_app

HEADER = '"Info","Profit","ID","Open time","Open Price","Close Time","Close Price","Type","Amount","Income"\n'


def csv_file(*trades):
    """trades: (id, income) pairs, all 200 stake on one asset, closing 3 Nov 11:26 IST."""
    body = "".join(f'"NZD/JPY (OTC)","85%","{i}","2026-11-03 11:24:02","89.477","2026-11-03 11:26:00",'
                   f'"89.458","Down","200","{inc}"\n' for i, inc in trades)
    return {"file": ("export.csv", (HEADER + body).encode(), "text/csv")}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    return TestClient(create_app("sqlite://"))


def signup(client, email="a@example.com", trader="94391218", consent=True):
    return client.post("/api/signup", json={"email": email, "password": "correct horse",
                                            "quotex_trader_id": trader, "consent": consent})


def auth(resp):
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def refer(client, *ids):
    return client.post("/api/admin/referred-traders", json={"trader_ids": list(ids)},
                       headers={"X-Admin-Token": "admin-secret"})


def upload(client, headers, *trades, account_type="live"):
    return client.post("/api/uploads", files=csv_file(*trades), data={"account_type": account_type},
                       headers=headers)


def test_signup_requires_consent(client):
    assert signup(client, consent=False).status_code == 422


def test_unreferred_user_can_log_in_but_is_blocked(client):
    r = signup(client)
    assert r.status_code == 201 and r.json()["verified"] is False
    assert upload(client, auth(r), ("t1", 370)).status_code == 403
    assert client.get("/api/stats/summary", headers=auth(r)).json()["detail"] == "not_referred"


def test_admin_verifies_trader_then_upload_works(client):
    r = signup(client)
    assert refer(client, "94391218").json() == {"added": 1, "already_present": 0}
    assert client.post("/api/login", json={"email": "a@example.com", "password": "correct horse"}).json()["verified"]
    out = upload(client, auth(r), ("t1", 370), ("t2", 0), ("t3", 200)).json()   # win, loss, draw
    assert (out["trades_in_file"], out["new"], out["already_had"]) == (3, 3, 0)


def test_reupload_adds_nothing(client):
    r = signup(client); refer(client, "94391218")
    upload(client, auth(r), ("t1", 370), ("t2", 0))
    out = upload(client, auth(r), ("t1", 370), ("t2", 0), ("t3", 370)).json()
    assert (out["new"], out["already_had"]) == (1, 2)


def test_daily_stats_and_summary(client):
    r = signup(client); refer(client, "94391218")
    upload(client, auth(r), ("t1", 370), ("t2", 0), ("t3", 200))
    day = client.get("/api/stats/daily", headers=auth(r)).json()
    assert day == [{"day": "2026-11-03", "trades": 3, "wins": 1, "losses": 1, "pnl": -30.0,
                    "staked": 600.0, "accuracy_pct": 50.0}]
    assert client.get("/api/stats/daily?start=2026-11-04", headers=auth(r)).json() == []
    s = client.get("/api/stats/summary", headers=auth(r)).json()
    assert (s["trades"], s["draws"], s["net_pnl"]) == (3, 1, -30.0)


def test_demo_and_live_are_kept_apart(client):
    r = signup(client); refer(client, "94391218")
    upload(client, auth(r), ("t1", 370), account_type="demo")
    assert client.get("/api/stats/summary?account_type=live", headers=auth(r)).json() == {"trades": 0}
    assert client.get("/api/stats/summary?account_type=demo", headers=auth(r)).json()["trades"] == 1


def test_users_cannot_see_each_others_trades(client):
    a = signup(client); b = signup(client, "b@example.com", "55512345")
    refer(client, "94391218", "55512345")
    upload(client, auth(a), ("t1", 370))
    upload(client, auth(b), ("t1", 0))              # same Quotex trade ID is fine for a different user
    assert client.get("/api/stats/summary", headers=auth(a)).json()["net_pnl"] == 170.0
    assert client.get("/api/stats/summary", headers=auth(b)).json()["net_pnl"] == -200.0


def test_duplicate_email_or_trader_id_rejected(client):
    signup(client)
    assert signup(client).status_code == 409
    assert signup(client, "other@example.com").status_code == 409


def test_wrong_password_and_missing_token(client):
    signup(client)
    assert client.post("/api/login", json={"email": "a@example.com", "password": "nope nope"}).status_code == 401
    assert client.get("/api/me").status_code == 401


def test_admin_endpoint_needs_the_token(client):
    assert client.post("/api/admin/referred-traders", json={"trader_ids": ["1"]}).status_code == 401
    assert client.post("/api/admin/referred-traders", json={"trader_ids": ["1"]},
                       headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_bad_file_gives_400(client):
    r = signup(client); refer(client, "94391218")
    bad = client.post("/api/uploads", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
                      data={"account_type": "live"}, headers=auth(r))
    assert bad.status_code == 400 and "Quotex" in bad.json()["detail"]
