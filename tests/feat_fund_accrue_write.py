"""永续乙闸 · 红测甲（费率计提案 basispad-02）。

复现链起笔：draft 费率快照走正式计提口。
口径取舍（当日锁册，见 docs/funding_accrue_decision.md）：走甲路径——
非 draft 或计提失败则整笔拒写，正式 ledger 零增，禁止影子草案混用。

本文件断言：
1. 仅 draft 快照可正式计提；计提成功后同票产生正式 ledger 行且快照转 accrued；
2. 非 draft 再计提整笔拒写，正式 ledger 零增；
3. 计提中途失败必须整笔回滚，draft 未过计提不得残留正式 ledger 行、
   仓位 realized_pnl 不得被提前改写（事故复现：台账已出现支付行而快照仍是 draft）；
4. 快照状态与 ledger 双写跨表对账：正式 ledger 行只允许挂 accrued 快照。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("BASISPAD_TEST", "1")

import db as dbmod  # noqa: E402
from app import app  # noqa: E402
import funding_svc  # noqa: E402


def _fresh_db():
    db = ROOT / "data" / "test_fund_accrue_write.db"
    if db.exists():
        db.unlink()
    dbmod.DB_PATH = db


pytestmark = pytest.mark.perp_gate_b


@pytest.fixture()
def client():
    _fresh_db()
    dbmod.init_db()
    from fastapi.testclient import TestClient as _TC

    with _TC(app, raise_server_exceptions=False) as c:
        yield c


def _login(client, username="trader", password="trader123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _new_draft(client, h, code, instrument_id=2, rate=0.0001):
    r = client.post(
        "/api/funding/snapshots",
        headers=h,
        json={
            "code": code,
            "instrument_id": instrument_id,
            "rate": rate,
            "mark_price": 3520.5,
            "period_start": 1000.0,
            "period_end": 2000.0,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _formal_ledger_count(snapshot_id):
    with dbmod.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM funding_ledgers WHERE snapshot_id=? AND status='accrued'",
            (snapshot_id,),
        ).fetchone()["c"]


def test_draft_accrue_writes_ledger_and_flips_snapshot(client):
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-A1")
    sid = snap["id"]
    assert snap["status"] == "draft"

    before = _formal_ledger_count(sid)
    r = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["ledgers"], "正式计提必须落正式 ledger 行"

    snaps = {s["id"]: s for s in client.get("/api/funding/snapshots", headers=h).json()}
    assert snaps[sid]["status"] == "accrued"
    assert _formal_ledger_count(sid) == before + len(r.json()["ledgers"])

    # 双写联检：正式 ledger 行只能挂 accrued 快照
    recon = client.get("/api/funding/reconcile", headers=h).json()
    assert recon["ok"] is True
    assert not [v for v in recon["violations"] if v.get("snapshot_id") == sid]


def test_non_draft_accrue_rejected_with_zero_ledger_growth(client):
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-A2")
    sid = snap["id"]
    r = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
    assert r.status_code == 200, r.text
    rows_after_first = _formal_ledger_count(sid)

    # 甲路径：非 draft 整笔拒写，正式 ledger 零增
    r2 = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
    assert r2.status_code == 409
    assert _formal_ledger_count(sid) == rows_after_first
    with dbmod.connect() as conn:
        assert conn.execute(
            "SELECT status FROM funding_snapshots WHERE id=?", (sid,)
        ).fetchone()["status"] == "accrued"


def test_failed_accrue_rolls_back_entire_write(client, monkeypatch):
    """事故复现：draft 未过计提却仍写入正式 ledger / 提前改写 realized_pnl。"""
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-A3")
    sid = snap["id"]
    with dbmod.connect() as conn:
        pos_before = {
            r["id"]: r["realized_pnl"]
            for r in conn.execute(
                "SELECT id, realized_pnl FROM positions WHERE instrument_id=? AND qty != 0",
                (snap["instrument_id"],),
            ).fetchall()
        }
    assert pos_before, "测试标的需要有持仓才能计提"

    real_payment = funding_svc.funding_payment

    def _boom(_qty, _rate, _mark):
        raise RuntimeError("模拟计提口在写入途中故障")

    monkeypatch.setattr(funding_svc, "funding_payment", _boom)
    try:
        r = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
        assert r.status_code == 500
    finally:
        monkeypatch.setattr(funding_svc, "funding_payment", real_payment)

    # 甲路径：整笔拒写——快照仍 draft、正式 ledger 零增、仓位已实现零改
    with dbmod.connect() as conn:
        assert conn.execute(
            "SELECT status FROM funding_snapshots WHERE id=?", (sid,)
        ).fetchone()["status"] == "draft"
        assert _formal_ledger_count(sid) == 0
        pos_after = {
            r["id"]: r["realized_pnl"]
            for r in conn.execute(
                "SELECT id, realized_pnl FROM positions WHERE instrument_id=? AND qty != 0",
                (snap["instrument_id"],),
            ).fetchall()
        }
    assert pos_after == pos_before, "计提失败不得提前改写策略簿已实现盈亏"


def test_cross_table_reconcile_flags_bypass_ledger_on_draft(client):
    """旁路落账：draft 快照下被塞入正式 ledger 行，跨表对账必须报警。"""
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-A4")
    sid = snap["id"]
    import time

    with dbmod.connect() as conn:
        conn.execute(
            "INSERT INTO funding_ledgers(code,snapshot_id,sleeve_id,instrument_id,position_qty,"
            "payment,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("FLG-BYPASS-001", sid, 1, snap["instrument_id"], -4, 7.041, "accrued", time.time()),
        )
        conn.commit()

    recon = client.get("/api/funding/reconcile", headers=h).json()
    assert recon["ok"] is False
    hit = [v for v in recon["violations"] if v.get("snapshot_id") == sid]
    assert hit, "draft 快照挂正式 ledger 行必须被双写联检查出"
