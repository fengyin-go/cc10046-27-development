"""代理池专用接口：冷却状态流转、冷却判定记录、分配与复测结果补录。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.proxy_pool import (
    DEFAULT_COOLDOWN_SECONDS,
    RECOVERY_IMMEDIATE,
    RECOVERY_RECHECK,
    ProxyPoolService,
)

router = APIRouter(prefix="/api/proxy-pool", tags=["代理池冷却调度"])

_proxy = ProxyPoolService()


@router.get("/cooldown/judgments")
def node_judgments(
    recovery_mode: str = RECOVERY_RECHECK,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """读取采集节点状态数据并返回节点冷却判定记录。"""
    return {
        "items": _proxy.domain_node_judgments(
            now_ts=now_ts, recovery_mode=recovery_mode),
        "recovery_mode": recovery_mode,
        "default_cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
    }


@router.post("/{node_id}/cooldown")
def cooldown_node(node_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行冷却：节点进入已冷却并登记服务端冷却开始时间。"""
    payload = payload or {}
    try:
        entry = _proxy.domain_cooldown_node(
            node_id,
            cooldown_seconds=payload.get("cooldown_seconds"),
            now_ts=payload.get("now_ts"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "message": "节点已进入冷却", "entry": entry}


@router.post("/{node_id}/recheck")
def submit_recheck(node_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """补录复测结果：通过恢复可用，未通过继续待检测。"""
    payload = payload or {}
    try:
        entry = _proxy.domain_submit_recheck(
            node_id,
            passed=bool(payload.get("passed", False)),
            now_ts=payload.get("now_ts"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "复测通过，节点恢复可分配" if payload.get("passed") else "复测未通过，节点继续待检测",
        "entry": entry,
    }


@router.post("/allocate")
def allocate_proxy(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """分配节点：返回实际取用节点、冷却判定记录、追踪号与排除明细。"""
    payload = payload or {}
    mode = payload.get("recovery_mode", RECOVERY_RECHECK)
    if mode not in (RECOVERY_RECHECK, RECOVERY_IMMEDIATE):
        raise HTTPException(status_code=400, detail=f"未知恢复模式：{mode}")
    return _proxy.domain_allocate_proxy(
        protocol=payload.get("protocol", "http"),
        region=payload.get("region", ""),
        trace_id=payload.get("trace_id", ""),
        recovery_mode=mode,
        now_ts=payload.get("now_ts"),
    )


@router.get("/errors/{trace_id}")
def errors_by_trace(trace_id: str) -> dict[str, Any]:
    """按追踪号取回无节点错误记录及当时的逐节点排除明细。"""
    items = _proxy.domain_find_errors_by_trace(trace_id)
    return {"trace_id": trace_id, "items": items, "total": len(items)}
