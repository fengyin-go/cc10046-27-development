"""代理池领域服务：节点冷却状态流转、冷却判定记录与带追踪号的分配。

状态流转（只允许在本服务内改状态）：
    可用 --冷却节点--> 已冷却(记录服务端冷却开始时间)
    已冷却 --冷却期满(复测门禁模式)--> 待检测 --复测通过--> 可用
                                        ＞--复测未过--> 仍为待检测
    已冷却 --冷却期满(立即恢复模式, 发生分配时)--> 可用
    已剔除：终态，任何流程都保持原状态，不参与分配

冷却判定记录由 domain_node_judgments 读取节点状态数据生成，分配链路只消费
判定记录，不直接猜测节点状态。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.services.error_logger import ErrorLoggerService

# 节点状态
ST_PENDING = "待检测"
ST_AVAILABLE = "可用"
ST_COOLING = "已冷却"
ST_REMOVED = "已剔除"

# 冷却期满后的恢复策略
RECOVERY_RECHECK = "recheck"        # 保留待检测，复测通过才恢复
RECOVERY_IMMEDIATE = "immediate"    # 期满即可恢复可分配状态并直接分配

DEFAULT_COOLDOWN_SECONDS = 30


class ProxyPoolService(BaseModuleService):
    MODULE = "proxy_pool"
    ENTITY = "代理节点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["节点标识", "代理地址", "代理协议", "可用状态", "响应延迟", "失败次数", "冷却秒数", "所属区域"]
    REQUIRED_FIELDS = ["节点标识", "代理地址", "代理协议"]
    STATUS_ORDER = [ST_PENDING, ST_AVAILABLE, ST_COOLING, ST_REMOVED]
    ACTION_RULES = {"检测节点": ST_PENDING, "冷却节点": ST_COOLING}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------ helpers
    def _now(self, now_ts: float | None) -> float:
        return time.time() if now_ts is None else float(now_ts)

    def _format_ts(self, ts: float | None) -> str:
        if ts is None:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _cooldown_seconds(self, entry: dict[str, Any]) -> int:
        """读取节点配置的冷却秒数；脏数据回退到默认 30 秒。"""
        raw = entry.get("冷却秒数")
        try:
            value = int(float(raw))
            return value if value > 0 else DEFAULT_COOLDOWN_SECONDS
        except (TypeError, ValueError):
            return DEFAULT_COOLDOWN_SECONDS

    def _new_trace_id(self) -> str:
        return f"TRACE-{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def _ensure_runtime_fields(self, entry: dict[str, Any]) -> None:
        entry.setdefault("冷却开始时间", None)
        entry.setdefault("冷却到期时间", None)
        entry.setdefault("复测状态", "")
        entry.setdefault("复测结果时间", None)
        entry.setdefault("恢复时间", None)
        entry.setdefault("最近追踪号", "")

    # ------------------------------------------------------------------ actions
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        from app.store import store
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "冷却节点":
            if entry.get("status") != ST_AVAILABLE:
                return None, f"仅「{ST_AVAILABLE}」节点可执行冷却，当前状态为{entry.get('status')}"
            return self.domain_cooldown_node(entry_id), f"{self.ENTITY}已冷却节点"
        if action == "检测节点":
            if entry.get("status") == ST_REMOVED:
                return None, "已剔除节点保持原状态，不能重新检测"
            entry["status"] = ST_PENDING
            entry["pending"] = True
            entry["abnormal"] = False
            self._ensure_runtime_fields(entry)
            entry["复测状态"] = "等待复测"
            return entry, f"{self.ENTITY}已检测节点"
        return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

    def domain_cooldown_node(
        self,
        entry_id: int,
        cooldown_seconds: int | None = None,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """执行冷却：节点进入已冷却，并按服务端时间登记冷却开始/到期时间。"""
        from app.store import store
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise ValueError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        if entry.get("status") == ST_REMOVED:
            raise ValueError("已剔除节点保持原状态，不能执行冷却")
        now_value = self._now(now_ts)
        seconds = int(cooldown_seconds) if cooldown_seconds and int(cooldown_seconds) > 0 \
            else self._cooldown_seconds(entry)
        self._ensure_runtime_fields(entry)
        entry["status"] = ST_COOLING
        entry["pending"] = True
        entry["abnormal"] = False
        entry["冷却开始时间"] = round(now_value, 3)
        entry["冷却到期时间"] = round(now_value + seconds, 3)
        entry["冷却秒数"] = seconds
        entry["复测状态"] = "等待复测"
        entry["恢复时间"] = None
        entry["可用状态"] = ST_COOLING
        return entry

    def domain_submit_recheck(
        self,
        entry_id: int,
        passed: bool,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """补录节点复测结果：通过则恢复可用，未通过则继续待检测。"""
        from app.store import store
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise ValueError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        if entry.get("status") == ST_REMOVED:
            raise ValueError("已剔除节点保持原状态，不接收复测结果")
        now_value = self._now(now_ts)
        self._ensure_runtime_fields(entry)
        entry["复测结果时间"] = round(now_value, 3)
        if entry.get("status") != ST_PENDING:
            # 已在其他流程恢复（例如立即恢复模式已分配）：仅留痕，不改状态
            entry["复测状态"] = "复测通过" if passed else "复测未过"
            return entry
        if passed:
            entry["status"] = ST_AVAILABLE
            entry["pending"] = True
            entry["abnormal"] = False
            entry["可用状态"] = ST_AVAILABLE
            entry["复测状态"] = "复测通过"
            entry["恢复时间"] = round(now_value, 3)
        else:
            entry["复测状态"] = "复测未过"
        return entry

    # ------------------------------------------------------------------ judgments
    def _judge_one(
        self,
        entry: dict[str, Any],
        now_value: float,
        recovery_mode: str,
    ) -> dict[str, Any]:
        self._ensure_runtime_fields(entry)
        status = entry.get("status")
        start = entry.get("冷却开始时间")
        expire = entry.get("冷却到期时间")
        seconds = self._cooldown_seconds(entry)
        elapsed = round(now_value - start, 3) if start is not None else 0.0
        remaining = max(0.0, round((expire - now_value), 3)) if expire is not None else 0.0
        expired = bool(expire is not None and now_value >= expire)

        reason = ""
        assignable = False
        if status == ST_AVAILABLE:
            reason = "可分配"
            assignable = True
        elif status == ST_COOLING:
            if not expired:
                reason = f"冷却中，剩余{remaining}秒，期满前不能被新请求取得"
            elif recovery_mode == RECOVERY_IMMEDIATE:
                reason = "冷却期满，可立即恢复分配"
                assignable = True
            else:
                # 复测门禁：期满读取时把节点转入待检测，继续等待复测结果
                entry["status"] = ST_PENDING
                entry["可用状态"] = ST_PENDING
                status = ST_PENDING
                reason = "冷却期满，等待复测结果，暂不可分配"
        if status == ST_PENDING:
            reason = f"等待复测结果，暂不可分配（复测状态：{entry.get('复测状态') or '等待复测'}）"
        elif status == ST_REMOVED:
            reason = "节点已剔除，保持原状态且不参与分配"

        return {
            "node_id": entry.get("id"),
            "节点标识": entry.get("节点标识"),
            "代理地址": entry.get("代理地址"),
            "代理协议": entry.get("代理协议"),
            "所属区域": entry.get("所属区域"),
            "status": status,
            "assignable": assignable,
            "reason": reason,
            "冷却秒数": seconds,
            "冷却开始时间": start,
            "冷却开始时间文本": self._format_ts(start),
            "冷却到期时间": expire,
            "冷却到期时间文本": self._format_ts(expire),
            "elapsed_seconds": elapsed,
            "remaining_seconds": remaining,
            "expired": expired,
            "复测状态": entry.get("复测状态", ""),
            "复测结果时间": entry.get("复测结果时间"),
            "恢复时间": entry.get("恢复时间"),
            "recovery_mode": recovery_mode,
        }

    def domain_node_judgments(
        self,
        now_ts: float | None = None,
        recovery_mode: str = RECOVERY_RECHECK,
    ) -> list[dict[str, Any]]:
        """读取采集节点状态数据，返回每个节点的冷却判定记录。"""
        from app.store import store
        now_value = self._now(now_ts)
        return [
            self._judge_one(entry, now_value, recovery_mode)
            for entry in store.rows(self.MODULE)
        ]

    # ------------------------------------------------------------------ allocation
    def domain_allocate_proxy(
        self,
        protocol: str = "http",
        region: str = "",
        trace_id: str = "",
        recovery_mode: str = RECOVERY_RECHECK,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        """按冷却判定记录分配节点；无节点可用时写入带追踪号和排除明细的错误记录。"""
        from app.store import store
        if recovery_mode not in (RECOVERY_RECHECK, RECOVERY_IMMEDIATE):
            recovery_mode = RECOVERY_RECHECK
        now_value = self._now(now_ts)
        trace = trace_id.strip() or self._new_trace_id()
        judgments = self.domain_node_judgments(now_value, recovery_mode)

        assignable = [j for j in judgments if j["assignable"]]
        matched = [
            j for j in assignable
            if (not protocol or str(j.get("代理协议", "")).casefold() == protocol.casefold())
            and (not region or region in str(j.get("所属区域", "")))
        ]
        pool = matched or assignable
        selected_judgment = pool[0] if pool else None

        selected_entry = None
        if selected_judgment is not None:
            selected_entry = store.find(self.MODULE, int(selected_judgment["node_id"]))
            self._ensure_runtime_fields(selected_entry)
            if selected_entry.get("status") == ST_COOLING:
                # 立即恢复模式：在分配瞬间恢复可分配状态
                selected_entry["status"] = ST_AVAILABLE
                selected_entry["可用状态"] = ST_AVAILABLE
                selected_entry["复测状态"] = "期满直接恢复"
                selected_entry["恢复时间"] = round(now_value, 3)
            selected_entry["最近追踪号"] = trace
            # 状态可能在本次分配中发生流转，重新生成判定记录，
            # 让详情反映本次实际采用的节点状态和计时。
            judgments = self.domain_node_judgments(now_value, recovery_mode)
            assignable = [j for j in judgments if j["assignable"]]
            selected_judgment = next(
                j for j in judgments if j["node_id"] == selected_entry["id"])

        excluded = [
            {
                "node_id": j["node_id"],
                "节点标识": j["节点标识"],
                "status": j["status"],
                "reason": j["reason"],
                "冷却开始时间": j["冷却开始时间"],
                "冷却开始时间文本": j["冷却开始时间文本"],
                "冷却到期时间": j["冷却到期时间"],
                "冷却到期时间文本": j["冷却到期时间文本"],
                "remaining_seconds": j["remaining_seconds"],
                "复测状态": j["复测状态"],
            }
            for j in judgments
            if not j["assignable"] or (selected_judgment and j["node_id"] != selected_judgment["node_id"])
        ]

        result: dict[str, Any] = {
            "allocated": selected_entry is not None,
            "proxy": selected_entry,
            "candidate_count": len(assignable),
            "protocol": protocol,
            "region": region,
            "trace_id": trace,
            "recovery_mode": recovery_mode,
            "recovery_mode_text": "期满待复测" if recovery_mode == RECOVERY_RECHECK else "期满立即恢复",
            "server_time": self._format_ts(now_value),
            "now_ts": round(now_value, 3),
            "selected_node_id": selected_entry["id"] if selected_entry else None,
            "judgments": judgments,
            "excluded": excluded,
        }

        if selected_entry is None:
            detail = json.dumps(excluded, ensure_ascii=False)
            summary = "；".join(f"{item['节点标识']}（{item['status']}：{item['reason']}）"
                                for item in excluded) or "全部节点不可分配"
            error_entry = ErrorLoggerService().domain_log_error(
                error_code="ERR-NO-PROXY",
                error_message=f"本次请求无可用代理节点。排除明细：{summary}",
                source_module=self.MODULE,
                trace_id=trace,
                severity="error",
                extra={"排除明细": detail},
            )
            result["error_log"] = error_entry
        else:
            result["error_log"] = None
        return result

    def domain_find_errors_by_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return ErrorLoggerService().domain_find_by_trace(trace_id)
