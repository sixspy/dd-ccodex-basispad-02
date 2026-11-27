"""变更与业务历史聚合。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from audit_svc import CONTENTION_ACTION
from auth import current_user
from db import connect

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/timeline")
def timeline(user=Depends(current_user)):
    with connect() as conn:
        events = []
        for r in conn.execute(
            "SELECT id, code, status, created_at, 'ticket' AS kind FROM tickets ORDER BY id DESC LIMIT 30"
        ).fetchall():
            events.append(dict(r))
        for r in conn.execute(
            "SELECT id, code, source AS status, created_at, 'fill' AS kind FROM fills ORDER BY id DESC LIMIT 30"
        ).fetchall():
            events.append(dict(r))
        for r in conn.execute(
            "SELECT id, code, status, created_at, 'funding' AS kind FROM funding_snapshots ORDER BY id DESC LIMIT 20"
        ).fetchall():
            events.append(dict(r))
        # 正式计提落账事件：与影子/草稿区分，便于交班联查
        for r in conn.execute(
            "SELECT l.id, l.code, l.status, l.created_at, 'funding_accrue' AS kind,"
            " l.snapshot_id, l.sleeve_id, l.instrument_id, l.payment"
            " FROM funding_ledgers l JOIN funding_snapshots s ON s.id=l.snapshot_id"
            " WHERE s.status IN ('accrued','settled') AND l.status IN ('accrued','settled')"
            " ORDER BY l.id DESC LIMIT 30"
        ).fetchall():
            events.append(dict(r))
        # 争用败方事件：只记录争用，不含正式落账
        for r in conn.execute(
            "SELECT id, entity_id AS code, action AS status, created_at, 'funding_contention' AS kind,"
            " actor, detail FROM audit_logs WHERE action=? ORDER BY id DESC LIMIT 30",
            (CONTENTION_ACTION,),
        ).fetchall():
            events.append(dict(r))
        for r in conn.execute(
            "SELECT id, code, status, created_at, 'settle' AS kind FROM settle_batches ORDER BY id DESC LIMIT 20"
        ).fetchall():
            events.append(dict(r))
        for r in conn.execute(
            "SELECT id, code, status, created_at, 'liq' AS kind FROM liquidation_queue ORDER BY id DESC LIMIT 20"
        ).fetchall():
            events.append(dict(r))
        events.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return events[:80]
