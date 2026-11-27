"""永续乙闸 · 红测乙（费率计提案 basispad-02）。

事故：盯市页把未升正的影子支付当成已计提，显示假绿；纸面簿里仓证方向与
资金费支付方向对不上。本文件断言：
1. 未正式计提（快照非 accrued）的支付行一律不得计入盯市正式资金费，不得假绿；
2. 正式计提后，盯市行必须带同票联查信息（计提行号 + 快照号 + 仓证边），
   且仓位已实现盈亏增量与正式台账支付同源同额；
3. 支付方向反号时，权益栏必须同步告警，告警点名仓证边与计提行号。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("BASISPAD_TEST", "1")

import db as dbmod  # noqa: E402
from app import app  # noqa: E402

pytestmark = pytest.mark.perp_gate_b


def _fresh_db():
    db = ROOT / "data" / "test_fund_mtm_fake.db"
    if db.exists():
        db.unlink()
    dbmod.DB_PATH = db


@pytest.fixture()
def client():
    _fresh_db()
    dbmod.init_db()
    with TestClient(app) as c:
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


def _mtm_row(client, h, instrument_id):
    rows = client.get("/api/marks/mtm", headers=h).json()
    hit = [r for r in rows if r["instrument_id"] == instrument_id]
    assert hit, "盯市行缺失"
    return hit[0]


def test_shadow_payment_on_draft_not_counted_as_accrued(client):
    """失败测乙：未升正影子支付不得在盯市页冒充已计提（假绿）。"""
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-B1")
    sid = snap["id"]
    now = time.time()
    with dbmod.connect() as conn:
        # 影子支付：快照仍是 draft，台账行只进影子态
        conn.execute(
            "INSERT INTO funding_ledgers(code,snapshot_id,sleeve_id,instrument_id,position_qty,"
            "payment,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("FLG-SHADOW-B1", sid, 1, snap["instrument_id"], -4, 1.4082, "shadow", now),
        )
        # 旁路行：台账自封 accrued 但快照仍 draft，同样不得计入正式
        conn.execute(
            "INSERT INTO funding_ledgers(code,snapshot_id,sleeve_id,instrument_id,position_qty,"
            "payment,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("FLG-BYPASS-B1", sid, 1, snap["instrument_id"], -4, 1.4082, "accrued", now),
        )
        conn.commit()

    row = _mtm_row(client, h, snap["instrument_id"])
    assert row["funding_formal_total"] == 0.0, "影子/旁路支付不得计入正式资金费"
    assert row["shadow_funding_count"] == 2, "未升正支付必须显式标灰而不是假绿"
    assert all(f["formal"] is False for f in row["funding_rows"])


def test_formal_accrue_is_linked_and_same_source_as_realized(client):
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-B2")
    sid = snap["id"]
    with dbmod.connect() as conn:
        before = {
            r["id"]: r["realized_pnl"]
            for r in conn.execute(
                "SELECT id, realized_pnl FROM positions WHERE instrument_id=? AND qty != 0",
                (snap["instrument_id"],),
            ).fetchall()
        }

    r = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
    assert r.status_code == 200, r.text
    codes = set(r.json()["ledgers"])

    row = _mtm_row(client, h, snap["instrument_id"])
    formal = [f for f in row["funding_rows"] if f["formal"]]
    assert formal, "正式计提必须在盯市行可见且不得与影子行混色"
    assert {f["ledger_code"] for f in formal} == codes
    # 同票联查：计提行号、快照号、仓证边（仓位 id + 带符号仓量）必须同票可点回
    for f in formal:
        assert f["snapshot_id"] == sid
        assert f["snapshot_status"] == "accrued"
        assert f["position_id"]
        assert f["position_side"] in ("long", "short")
        assert f["position_qty"] != 0
    assert row["funding_formal_total"] == pytest.approx(sum(f["payment"] for f in formal))

    # 完成定义：仓位已实现与支付同源同额
    with dbmod.connect() as conn:
        after = {
            r["id"]: r["realized_pnl"]
            for r in conn.execute(
                "SELECT id, realized_pnl FROM positions WHERE instrument_id=? AND qty != 0",
                (snap["instrument_id"],),
            ).fetchall()
        }
    delta_by_pos = {pid: after[pid] - before[pid] for pid in before}
    for f in formal:
        assert delta_by_pos[f["position_id"]] == pytest.approx(f["payment"])


def test_reversed_payment_direction_raises_equity_alert(client):
    """支付方向反号：权益栏同步告警，点名仓证边与计提行号。"""
    h = _login(client)
    # 正费率多头：期望支付为负（-qty*mark*rate），反号行故意落正数
    snap = _new_draft(client, h, "FND-RED-B3", instrument_id=1, rate=0.0001)
    sid = snap["id"]
    r = client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)
    assert r.status_code == 200, r.text

    with dbmod.connect() as conn:
        pos = conn.execute(
            "SELECT * FROM positions WHERE instrument_id=? AND qty > 0 LIMIT 1",
            (snap["instrument_id"],),
        ).fetchone()
        assert pos, "反号场景需要正费率多头仓位"
        # 制造反号：多头(qty>0)正费率下支付却为正（纸面反号）
        conn.execute(
            "INSERT INTO funding_ledgers(code,snapshot_id,sleeve_id,instrument_id,position_qty,"
            "payment,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("FLG-REV-B3", sid, pos["sleeve_id"], snap["instrument_id"], pos["qty"], 9.99, "accrued", time.time()),
        )
        conn.commit()
        sleeve_id = pos["sleeve_id"]
        position_id = pos["id"]
        side_word = "多头"

    row = _mtm_row(client, h, snap["instrument_id"])
    rev = [f for f in row["funding_rows"] if f["ledger_code"] == "FLG-REV-B3"][0]
    assert rev["direction_alert"] is True
    assert rev["position_side"] == "long"

    detail = client.get(f"/api/sleeves/{sleeve_id}", headers=h).json()
    target = [p for p in detail["positions"] if p["id"] == position_id][0]
    alerts = target["funding_alerts"]
    hit = [a for a in alerts if a["ledger_code"] == "FLG-REV-B3"]
    assert hit, "纸面簿必须带出反号告警"
    msg = hit[0]["message"]
    assert "FLG-REV-B3" in msg and side_word in msg
    assert detail["equity_alert"] is True, "支付方向反号时权益栏必须同步告警"
