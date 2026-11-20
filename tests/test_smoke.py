
import threading

import pytest
from fastapi.testclient import TestClient

from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture
def h():
    c = TestClient(app)
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + token}


def _formal_states(client, hdr):
    return {(x["mapped_from"], x["entity_key"]) for x in client.get("/api/formal", headers=hdr).json()["items"]}


def _revoke_conflicts(client, hdr, old_key):
    return [x for x in client.get("/api/conflicts", headers=hdr).json()["items"]
            if x["old_key"] == old_key and x["reason"] == "撤映"]


def test_remap_and_conflict():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "实体键桥"
    h = {"Authorization": "Bearer " + c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]}
    assert c.post("/api/remap", headers=h, json={"old_key": "OLD-B2"}).json()["entity_key"] == "NEW-200"
    assert c.post("/api/remap", headers=h, json={"old_key": "OLD-ZZ"}).status_code == 409
    assert any(x["old_key"] == "OLD-ZZ" for x in c.get("/api/conflicts", headers=h).json()["items"])


def test_revoke_leaves_no_orphan(h, monkeypatch):
    """蒸发失败测（撤映后正式与冲突两边皆空、映射册/互证卡仍在册即蒸发）。"""
    c = TestClient(app)
    assert ("OLD-A1", "NEW-100") in _formal_states(c, h)

    r = c.post("/api/revoke", headers=h, json={"old_key": "OLD-A1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("orphaned") is False, "撤映不得留孤儿：须迁冲突或留标（甲乙二者取一）"

    # 四处落点都要有痕：正式区消失、冲突区出现撤映记录、映射册状态改为已撤映
    assert ("OLD-A1", "NEW-100") not in _formal_states(c, h)
    pending = _revoke_conflicts(c, h, "OLD-A1")
    assert pending, "撤映须在同事务迁入冲突区待处置，不得无痕删除"
    assert pending[0]["status"] == "待处置"

    reg = {x["old_key"]: x for x in c.get("/api/register", headers=h).json()["items"]}
    assert reg["OLD-A1"]["state"] == "已撤映"
    assert reg["OLD-A1"]["new_key"] == "NEW-100"

    cards = {x["old_key"]: x for x in c.get("/api/reconcile-cards", headers=h).json()["items"]}
    assert cards["OLD-A1"]["formal_state"] == "不在正式"
    assert cards["OLD-A1"]["conflict_state"] == "在冲突"
    assert cards["OLD-A1"]["register_state"] == "已撤映"
    assert cards["OLD-A1"]["verdict"] == "一致"

    audits = [x for x in c.get("/api/audit", headers=h).json()["items"] if x["old_key"] == "OLD-A1"]
    assert any(x["action"] == "撤映" and x["outcome"] == "成功" for x in audits)

    assert c.get("/api/orphans", headers=h).json()["count"] == 0
    assert c.get("/api/reconcile-cards", headers=h).json()["inconsistent"] == 0


def test_concurrent_revoke_late_loser_marked_contended(h):
    """抢撤争用失败测：两路同撤同一实体，后到者须报争用且不得二次删除/重复迁入。"""
    c = TestClient(app)
    barrier = threading.Barrier(2)
    results = []

    def worker():
        cli = TestClient(app)
        barrier.wait()
        resp = cli.post("/api/revoke", headers=h, json={"old_key": "OLD-A1"})
        results.append(resp)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], f"两路抢撤须一成一争用，实际 {codes}"

    loser = next(r for r in results if r.status_code == 409)
    assert loser.json().get("contended") is True, "后到者须显式记争用"
    assert "争用" in loser.json().get("detail", "")

    pending = _revoke_conflicts(c, h, "OLD-A1")
    assert len(pending) == 1, "争用后到者不得重复迁入冲突"
    audits = [x for x in c.get("/api/audit", headers=h).json()["items"] if x["old_key"] == "OLD-A1"]
    assert sum(1 for x in audits if x["action"] == "撤映" and x["outcome"] == "成功") == 1
    assert any(x["action"] == "撤映" and x["outcome"] == "争用" for x in audits)
    cards = {x["old_key"]: x for x in c.get("/api/reconcile-cards", headers=h).json()["items"]}
    assert cards["OLD-A1"]["verdict"] == "一致"
    assert c.get("/api/reconcile-cards", headers=h).json()["inconsistent"] == 0
    assert c.get("/api/orphans", headers=h).json()["count"] == 0

    # 锁已释放：重映后再次撤映应成功，不得被残留锁判成争用。
    assert c.post("/api/remap", headers=h, json={"old_key": "OLD-A1"}).status_code == 200
    again = c.post("/api/revoke", headers=h, json={"old_key": "OLD-A1"})
    assert again.status_code == 200, again.text
    assert c.get("/api/reconcile-cards", headers=h).json()["inconsistent"] == 0
    assert c.get("/api/orphans", headers=h).json()["count"] == 0


def test_revoke_rolls_back_when_conflict_insert_fails(h):
    """甲路径回滚测：撤映事务内迁冲突失败，则正式区删除必须一并回滚。"""
    c = TestClient(app)
    assert ("OLD-A1", "NEW-100") in _formal_states(c, h)

    import app as appmod
    real_refresh = appmod._refresh_card

    def boom(*a, **k):
        raise RuntimeError("注入失败：迁冲突后置步骤失败")

    monkeypatch_ok = False
    appmod._refresh_card = boom
    try:
        r = c.post("/api/revoke", headers=h, json={"old_key": "OLD-A1"})
        assert r.status_code == 500, r.text
        # 事务回滚：正式区仍在，冲突区没有被单独落下的撤映记录
        assert ("OLD-A1", "NEW-100") in _formal_states(c, h)
        assert not _revoke_conflicts(c, h, "OLD-A1")
        reg = {x["old_key"]: x for x in c.get("/api/register", headers=h).json()["items"]}
        assert reg["OLD-A1"]["state"] == "在册"
        assert c.get("/api/orphans", headers=h).json()["count"] == 0
        # 失败审计独立落盘，可追溯
        audits = [x for x in c.get("/api/audit", headers=h).json()["items"] if x["old_key"] == "OLD-A1"]
        assert any(x["action"] == "撤映" and x["outcome"] == "失败" for x in audits)
    finally:
        appmod._refresh_card = real_refresh
