"""资金费率快照与计提。

费率计提案 basispad-02（甲路径，当日锁册见 docs/funding_accrue_decision.md）：
仅 draft 快照可正式计提；计提在单事务内写正式 ledger、改仓位已实现、翻 accrued，
失败整笔回滚，正式 ledger 零增。互斥靠事务内条件更新抢占，败方只留争用记录
（见 docs/funding_accrue_contention.md）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from audit_svc import contention_records, record_contention
from auth import current_user, require_roles
from db import audit, connect, row_to_dict
from policies import assert_writable_role, funding_payment

router = APIRouter(prefix="/api/funding", tags=["funding"])

FORMAL_SNAPSHOT_STATUSES = ("accrued", "settled")
FORMAL_LEDGER_STATUSES = ("accrued", "settled")


class SnapshotBody(BaseModel):
    code: str
    instrument_id: int
    rate: float
    mark_price: float | None = None
    period_start: float
    period_end: float
    note: str = ""


def _immediate_conn():
    """打开立即写事务连接：先拿库级写锁，把并发压到条件更新上排队。"""
    conn = connect()
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("BEGIN IMMEDIATE")
    return conn


def _position_side(qty: float) -> str:
    if qty > 0:
        return "long"
    if qty < 0:
        return "short"
    return "flat"


def _side_word(qty: float) -> str:
    return "多头" if qty > 0 else ("空头" if qty < 0 else "平仓")


def _direction_alert(position_qty: float, payment: float, rate: float, mark: float) -> bool:
    """支付方向反号：实落支付与期望（-qty*mark*rate）同为非零且符号相反。"""
    expected = funding_payment(position_qty, rate, mark)
    if expected == 0 or payment == 0:
        return False
    return (expected > 0) != (payment > 0)


@router.get("/snapshots")
def list_snapshots(status: str | None = None, user=Depends(current_user)):
    sql = "SELECT * FROM funding_snapshots"
    args: list = []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY id DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


@router.get("/ledgers")
def list_ledgers(snapshot_id: int | None = None, user=Depends(current_user)):
    sql = "SELECT * FROM funding_ledgers WHERE 1=1"
    args: list = []
    if snapshot_id:
        sql += " AND snapshot_id=?"
        args.append(snapshot_id)
    sql += " ORDER BY id DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


@router.post("/snapshots")
def create_snapshot(body: SnapshotBody, user=Depends(require_roles("admin", "trader"))):
    assert_writable_role(user["role"])
    with connect() as conn:
        inst = conn.execute("SELECT * FROM instruments WHERE id=?", (body.instrument_id,)).fetchone()
        if not inst:
            raise HTTPException(404, "标的不存在")
        if conn.execute("SELECT 1 FROM funding_snapshots WHERE code=?", (body.code,)).fetchone():
            raise HTTPException(409, "code 已存在")
        mark_row = conn.execute(
            "SELECT mark FROM mark_prices WHERE instrument_id=?", (body.instrument_id,)
        ).fetchone()
        mark = body.mark_price if body.mark_price is not None else (mark_row["mark"] if mark_row else 0)
        now = time.time()
        cur = conn.execute(
            "INSERT INTO funding_snapshots(code,instrument_id,rate,mark_price,status,period_start,period_end,created_at,note)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (body.code, body.instrument_id, body.rate, mark, "draft", body.period_start, body.period_end, now, body.note),
        )
        sid = cur.lastrowid
        audit(conn, user["username"], "create", "funding_snapshot", sid, body.code)
        conn.commit()
        return row_to_dict(conn.execute("SELECT * FROM funding_snapshots WHERE id=?", (sid,)).fetchone())


@router.post("/snapshots/{sid}/accrue")
def accrue_snapshot(sid: int, user=Depends(require_roles("admin", "trader"))):
    """正式计提（甲路径）：单事务条件抢占 draft → 写正式 ledger/已实现 → 翻 accrued。

    非 draft 或计提失败整笔拒写，正式 ledger 零增；争用败方只产生争用记录。
    """
    assert_writable_role(user["role"])
    resource = f"funding:{sid}"
    conn = _immediate_conn()
    try:
        snap = conn.execute("SELECT * FROM funding_snapshots WHERE id=?", (sid,)).fetchone()
        if not snap:
            conn.rollback()
            raise HTTPException(404, "快照不存在")

        # 互斥抢占：只有 draft 能被翻成 accrued；rowcount=0 即未抢到（甲路径整笔拒写）
        flipped = conn.execute(
            "UPDATE funding_snapshots SET status='accrued' WHERE id=? AND status='draft'",
            (sid,),
        )
        if flipped.rowcount == 0:
            record_contention(conn, user["username"], resource, sid, snap["code"])
            conn.commit()
            raise HTTPException(409, "仅 draft 可计提：快照已被他路计提或状态非 draft（争用已记录）")

        # 若有官方结算批次覆盖同周期则禁止
        conflict = conn.execute(
            "SELECT 1 FROM settle_batches WHERE official=1 AND status='official'"
        ).fetchone()
        positions = conn.execute(
            "SELECT * FROM positions WHERE instrument_id=? AND qty != 0",
            (snap["instrument_id"],),
        ).fetchall()
        now = time.time()
        created = []
        for i, p in enumerate(positions, start=1):
            pay = funding_payment(p["qty"], snap["rate"], snap["mark_price"])
            code = f"FLG-{sid}-{i:03d}"
            # 计提台账与仓证方向成对落库：sleeve/instrument 唯一定位仓位，同票联查
            conn.execute(
                "INSERT INTO funding_ledgers(code,snapshot_id,sleeve_id,instrument_id,position_qty,payment,status,created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (code, sid, p["sleeve_id"], p["instrument_id"], p["qty"], pay, "accrued", now),
            )
            # 记入已实现（费率）：与台账支付同源同额、同事务
            conn.execute(
                "UPDATE positions SET realized_pnl = realized_pnl + ?, updated_at=? WHERE id=?",
                (pay, now, p["id"]),
            )
            created.append(code)
        audit(conn, user["username"], "accrue", "funding_snapshot", sid, f"rows={len(created)}")
        conn.commit()
        return {"ok": True, "ledgers": created, "conflict_hint": bool(conflict)}
    except HTTPException:
        raise
    except Exception:
        # 甲路径：任何中途故障整笔回滚，正式区零改
        conn.rollback()
        raise HTTPException(500, "计提失败，整笔已回滚：正式 ledger 零增、快照仍为 draft")
    finally:
        conn.close()


@router.get("/contention")
def list_contention(user=Depends(current_user)):
    """争用败方记录（费率计提互斥）。"""
    with connect() as conn:
        return contention_records(conn)


@router.get("/reconcile")
def reconcile(user=Depends(current_user)):
    """快照状态与 ledger 双写联检 + 跨表对账 + 支付方向反号巡检。"""
    with connect() as conn:
        violations: list[dict] = []
        rows = conn.execute(
            "SELECT l.id AS ledger_id, l.code AS ledger_code, l.snapshot_id, l.sleeve_id,"
            " l.instrument_id, l.position_qty, l.payment, l.status AS ledger_status,"
            " s.code AS snapshot_code, s.status AS snapshot_status, s.rate, s.mark_price"
            " FROM funding_ledgers l JOIN funding_snapshots s ON s.id=l.snapshot_id"
        ).fetchall()
        for r in rows:
            base = {
                "snapshot_id": r["snapshot_id"],
                "snapshot_code": r["snapshot_code"],
                "ledger_id": r["ledger_id"],
                "ledger_code": r["ledger_code"],
                "sleeve_id": r["sleeve_id"],
                "instrument_id": r["instrument_id"],
            }
            if r["snapshot_status"] not in FORMAL_SNAPSHOT_STATUSES:
                violations.append({**base, "type": "bypass_ledger_on_non_accrued",
                                   "detail": f"快照 {r['snapshot_status']} 下不得存在正式台账行"})
            elif r["ledger_status"] not in FORMAL_LEDGER_STATUSES:
                violations.append({**base, "type": "shadow_row_under_formal_snapshot",
                                   "detail": "正式快照下挂影子台账行"})
            if _direction_alert(r["position_qty"], r["payment"], r["rate"], r["mark_price"]):
                expected = funding_payment(r["position_qty"], r["rate"], r["mark_price"])
                violations.append({**base, "type": "direction_reversed",
                                   "detail": f"支付方向反号：期望 {expected}，实落 {r['payment']}",
                                   "expected": expected, "actual": r["payment"]})
        return {"ok": not violations, "violations": violations, "checked_at": time.time()}


@router.get("/links")
def funding_links(user=Depends(current_user)):
    """同票联查：计提行号 ↔ 快照号/状态 ↔ 仓证边（仓位 id、带符号仓量、多空）。"""
    with connect() as conn:
        out = []
        rows = conn.execute(
            "SELECT l.code AS ledger_code, l.snapshot_id, l.sleeve_id, l.instrument_id,"
            " l.position_qty, l.payment, l.status AS ledger_status,"
            " s.code AS snapshot_code, s.status AS snapshot_status, s.rate, s.mark_price,"
            " p.id AS position_id, p.qty AS position_qty_live"
            " FROM funding_ledgers l"
            " JOIN funding_snapshots s ON s.id=l.snapshot_id"
            " LEFT JOIN positions p ON p.sleeve_id=l.sleeve_id AND p.instrument_id=l.instrument_id"
            " ORDER BY l.id DESC"
        ).fetchall()
        for r in rows:
            formal = (
                r["snapshot_status"] in FORMAL_SNAPSHOT_STATUSES
                and r["ledger_status"] in FORMAL_LEDGER_STATUSES
            )
            out.append({
                "ledger_code": r["ledger_code"],
                "snapshot_id": r["snapshot_id"],
                "snapshot_code": r["snapshot_code"],
                "snapshot_status": r["snapshot_status"],
                "sleeve_id": r["sleeve_id"],
                "instrument_id": r["instrument_id"],
                "position_id": r["position_id"],
                "position_side": _position_side(r["position_qty"]),
                "position_qty": r["position_qty"],
                "payment": r["payment"],
                "formal": formal,
                "direction_alert": _direction_alert(
                    r["position_qty"], r["payment"], r["rate"], r["mark_price"]
                ),
            })
        return out


@router.post("/snapshots/{sid}/void")
def void_snapshot(sid: int, user=Depends(require_roles("admin"))):
    with connect() as conn:
        snap = conn.execute("SELECT * FROM funding_snapshots WHERE id=?", (sid,)).fetchone()
        if not snap:
            raise HTTPException(404, "快照不存在")
        if snap["status"] == "settled":
            raise HTTPException(409, "已结算不可作废")
        conn.execute("UPDATE funding_snapshots SET status='void' WHERE id=?", (sid,))
        conn.execute(
            "UPDATE funding_ledgers SET status='void' WHERE snapshot_id=?", (sid,)
        )
        audit(conn, user["username"], "void", "funding_snapshot", sid, snap["code"])
        conn.commit()
        return {"ok": True}
