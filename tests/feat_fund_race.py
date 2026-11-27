"""永续乙闸 · 红测丙（费率计提案 basispad-02）。

争用段（与 docs/funding_accrue_decision.md 取舍段物理分开，见
docs/funding_accrue_contention.md）：两路写口争「费率计提互斥」，
未抢到的一方只产生争用记录，禁止无锁双成。

对打压测脚本点名：race_funding_accrue。
"""
from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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
    db = ROOT / "data" / "test_fund_race.db"
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


def test_race_funding_accrue(client):
    """两路同票并发正式计提：恰好一路成功，败方只留争用记录。"""
    h = _login(client)
    snap = _new_draft(client, h, "FND-RED-C1")
    sid = snap["id"]

    with dbmod.connect() as conn:
        expected_rows = conn.execute(
            "SELECT COUNT(*) c FROM positions WHERE instrument_id=? AND qty != 0",
            (snap["instrument_id"],),
        ).fetchone()["c"]
    assert expected_rows > 0

    barrier = threading.Barrier(2)

    def _accrue():
        barrier.wait()
        return client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _accrue(), range(2)))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409], f"互斥失效：实际状态 {statuses}"
    ok = [r for r in results if r.status_code == 200][0]
    assert len(ok.json()["ledgers"]) == expected_rows

    # 正式台账只落一份：双成会产生 2*expected_rows 行
    with dbmod.connect() as conn:
        ledger_rows = conn.execute(
            "SELECT COUNT(*) c FROM funding_ledgers WHERE snapshot_id=? AND status='accrued'",
            (sid,),
        ).fetchone()["c"]
        assert ledger_rows == expected_rows
        # 快照只被翻一次
        assert conn.execute(
            "SELECT status FROM funding_snapshots WHERE id=?", (sid,)
        ).fetchone()["status"] == "accrued"
        # 仓位已实现只被加一次
        realized_sum = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) s FROM positions WHERE instrument_id=? AND qty != 0",
            (snap["instrument_id"],),
        ).fetchone()["s"]

    # 败方只产生争用记录，不得有第二份正式落账
    contentions = client.get("/api/funding/contention", headers=h).json()
    assert isinstance(contentions, list)
    hit = [c for c in contentions if isinstance(c, dict) and c.get("snapshot_id") == sid]
    assert hit, "争用败方必须留争用记录"
    assert all(c["action"] == "funding_accrue_contention" for c in hit)
    assert all(c["resource"] == f"funding:{sid}" for c in hit)

    recon = client.get("/api/funding/reconcile", headers=h).json()
    sid_violations = [v for v in recon["violations"] if v.get("snapshot_id") == sid]
    assert not sid_violations, f"本快照双写后跨表对账必须一致：{sid_violations}"

    # 已升正快照再被两路打：全部 409，正式零增、争用仍记录
    barrier2 = threading.Barrier(2)

    def _reaccrue():
        barrier2.wait()
        return client.post(f"/api/funding/snapshots/{sid}/accrue", headers=h)

    with ThreadPoolExecutor(max_workers=2) as pool:
        again = list(pool.map(lambda _: _reaccrue(), range(2)))
    assert sorted(r.status_code for r in again) == [409, 409]
    with dbmod.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM funding_ledgers WHERE snapshot_id=? AND status='accrued'",
            (sid,),
        ).fetchone()["c"] == expected_rows
        assert conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) s FROM positions WHERE instrument_id=? AND qty != 0",
            (snap["instrument_id"],),
        ).fetchone()["s"] == realized_sum
