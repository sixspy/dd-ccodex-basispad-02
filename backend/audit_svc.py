"""审计只读与争用记录。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import current_user
from db import audit, connect

router = APIRouter(prefix="/api/audit", tags=["audit"])

CONTENTION_ACTION = "funding_accrue_contention"
_CONTENTION_ENTITY = "funding_snapshot"


class _ContentionDetail(BaseModel):
    resource: str
    snapshot_code: str


def record_contention(conn, actor: str, resource: str, snapshot_id: int, snapshot_code: str) -> None:
    """费率计提互斥败方记录：争用段唯一允许的写入，不含任何正式台账落字。"""
    detail = f"resource={resource}; snapshot={snapshot_code}; 未抢到计提权，正式 ledger 零增"
    audit(conn, actor, CONTENTION_ACTION, _CONTENTION_ENTITY, snapshot_id, detail)


def _parse(detail: str) -> dict:
    parsed: dict = {}
    for part in str(detail).split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            parsed[k.strip()] = v.strip()
    return parsed


def contention_records(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE action=? AND entity=? ORDER BY id DESC",
        (CONTENTION_ACTION, _CONTENTION_ENTITY),
    ).fetchall()
    out = []
    for r in rows:
        meta = _parse(r["detail"])
        out.append(
            {
                "id": r["id"],
                "action": r["action"],
                "actor": r["actor"],
                "entity": r["entity"],
                "snapshot_id": int(r["entity_id"]),
                "resource": meta.get("resource", ""),
                "snapshot_code": meta.get("snapshot", ""),
                "detail": r["detail"],
                "created_at": r["created_at"],
            }
        )
    return out


@router.get("")
def list_audit(limit: int = 100, user=Depends(current_user)):
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


@router.get("/contention")
def list_funding_contention(user=Depends(current_user)):
    with connect() as conn:
        return contention_records(conn)
