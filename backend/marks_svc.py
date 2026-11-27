"""标记价与基差。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import current_user, require_roles
from db import audit, connect, row_to_dict
from policies import assert_writable_role, funding_payment, unrealized_pnl

router = APIRouter(prefix="/api/marks", tags=["marks"])

FORMAL_SNAPSHOT_STATUSES = ("accrued", "settled")
FORMAL_LEDGER_STATUSES = ("accrued", "settled")


def _position_side(qty: float) -> str:
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return "flat"


def _direction_alert(position_qty: float, payment: float, rate: float, mark: float) -> bool:
    expected = funding_payment(position_qty, rate, mark)
    if expected == 0 or payment == 0:
        return False
    return (expected > 0) != (payment > 0)


class MarkBody(BaseModel):
    instrument_id: int
    mark: float
    index_price: float
    status: str = "live"


@router.get("")
def list_marks(user=Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.*, i.symbol FROM mark_prices m JOIN instruments i ON i.id=m.instrument_id ORDER BY m.instrument_id"
        ).fetchall()
        return [dict(r) for r in rows]


@router.post("/upsert")
def upsert_mark(body: MarkBody, user=Depends(require_roles("admin", "trader"))):
    assert_writable_role(user["role"])
    with connect() as conn:
        inst = conn.execute("SELECT * FROM instruments WHERE id=?", (body.instrument_id,)).fetchone()
        if not inst:
            raise HTTPException(404, "标的不存在")
        # 结算锁定时禁止改标记价
        if conn.execute("SELECT 1 FROM settle_batches WHERE status='locked'").fetchone():
            raise HTTPException(409, "结算批次锁定中，禁止改标记价")
        basis = 0.0
        if body.index_price:
            basis = (body.mark - body.index_price) / body.index_price * 10000.0
        now = time.time()
        conn.execute(
            "INSERT INTO mark_prices(instrument_id,mark,index_price,basis_bps,status,as_of)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET"
            " mark=excluded.mark, index_price=excluded.index_price, basis_bps=excluded.basis_bps,"
            " status=excluded.status, as_of=excluded.as_of",
            (body.instrument_id, body.mark, body.index_price, basis, body.status, now),
        )
        audit(conn, user["username"], "upsert", "mark", body.instrument_id, f"mark={body.mark}")
        conn.commit()
        return row_to_dict(
            conn.execute("SELECT * FROM mark_prices WHERE instrument_id=?", (body.instrument_id,)).fetchone()
        )


@router.get("/mtm")
def mtm_report(user=Depends(current_user)):
    """盯市：未实现 + 正式资金费分账。

    只有 accrued/settled 快照下的正式台账行才计入 funding_formal_total；
    draft 快照的影子/旁路行只进 shadow 计数并显式 formal=False，禁止假绿。
    每行带同票联查字段（计提行号、快照号、仓证边）与方向反号标记。
    """
    with connect() as conn:
        out = []
        for p in conn.execute("SELECT * FROM positions WHERE qty != 0").fetchall():
            mark = conn.execute(
                "SELECT mark FROM mark_prices WHERE instrument_id=?", (p["instrument_id"],)
            ).fetchone()
            m = mark["mark"] if mark else p["avg_entry"]
            funding_rows = []
            ledger_rows = conn.execute(
                "SELECT l.code AS ledger_code, l.snapshot_id, l.payment, l.status AS ledger_status,"
                " l.position_qty, s.code AS snapshot_code, s.status AS snapshot_status,"
                " s.rate, s.mark_price"
                " FROM funding_ledgers l JOIN funding_snapshots s ON s.id=l.snapshot_id"
                " WHERE l.sleeve_id=? AND l.instrument_id=?",
                (p["sleeve_id"], p["instrument_id"]),
            ).fetchall()
            for lr in ledger_rows:
                formal = (
                    lr["snapshot_status"] in FORMAL_SNAPSHOT_STATUSES
                    and lr["ledger_status"] in FORMAL_LEDGER_STATUSES
                )
                funding_rows.append(
                    {
                        "ledger_code": lr["ledger_code"],
                        "snapshot_id": lr["snapshot_id"],
                        "snapshot_code": lr["snapshot_code"],
                        "snapshot_status": lr["snapshot_status"],
                        "position_id": p["id"],
                        "position_side": _position_side(p["qty"]),
                        "position_qty": p["qty"],
                        "payment": lr["payment"],
                        "formal": formal,
                        "direction_alert": _direction_alert(
                            p["qty"], lr["payment"], lr["rate"], lr["mark_price"]
                        ),
                    }
                )
            formal_rows = [f for f in funding_rows if f["formal"]]
            out.append(
                {
                    "sleeve_id": p["sleeve_id"],
                    "instrument_id": p["instrument_id"],
                    "qty": p["qty"],
                    "avg_entry": p["avg_entry"],
                    "mark": m,
                    "upnl": unrealized_pnl(p["qty"], p["avg_entry"], m),
                    "funding_rows": funding_rows,
                    "funding_formal_total": sum(f["payment"] for f in formal_rows),
                    "shadow_funding_count": sum(1 for f in funding_rows if not f["formal"]),
                    "direction_alert": any(f["direction_alert"] for f in formal_rows),
                }
            )
        return out


@router.post("/{instrument_id}/freeze")
def freeze_mark(instrument_id: int, user=Depends(require_roles("admin"))):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM mark_prices WHERE instrument_id=?", (instrument_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "无标记价")
        conn.execute(
            "UPDATE mark_prices SET status='frozen' WHERE instrument_id=?", (instrument_id,)
        )
        audit(conn, user["username"], "freeze", "mark", instrument_id, "frozen")
        conn.commit()
        return {"ok": True, "status": "frozen"}
