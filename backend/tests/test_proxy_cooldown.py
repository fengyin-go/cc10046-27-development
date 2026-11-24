"""代理池冷却到期恢复状态流转测试。

覆盖值班场景：
- 冷却后进入已冷却，按服务端冷却开始时间计时，期满前不能被新请求取得；
- 冷却判定记录读取节点状态数据，可靠性链路消费判定记录；
- 第 30 秒冷却期满 + 复测结果迟到的两种状态：立即恢复分配 vs 待检测等复测；
- 第 29、30 秒检查 + 补复测，对照三次实际返回节点；
- 无节点可用时错误记录关联追踪号和逐节点排除明细，恢复后按追踪号可复盘；
- 甲乙分别计时；已剔除节点保持原状态；恢复后正常检查可再次分配。
"""
from __future__ import annotations

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.proxy_pool import (
    RECOVERY_IMMEDIATE,
    RECOVERY_RECHECK,
    ProxyPoolService,
)
from app.services.error_logger import ErrorLoggerService
from app.routers.chains import run_reliability

T0 = 1_000.0  # 固定时钟起点，避免依赖真实时间


def _node(node_id: int, name: str, status: str) -> dict:
    return {
        "id": node_id,
        "status": status,
        "pending": status != "已剔除",
        "abnormal": status == "已剔除",
        "节点标识": name,
        "代理地址": f"10.10.1.{node_id}:8080",
        "代理协议": "http",
        "可用状态": status,
        "响应延迟": "80",
        "失败次数": "0",
        "冷却秒数": 30,
        "所属区域": "华东",
        "冷却开始时间": None,
        "冷却到期时间": None,
        "复测状态": "",
        "复测结果时间": None,
        "恢复时间": None,
        "最近追踪号": "",
    }


@pytest.fixture(autouse=True)
def reset_proxy_tables():
    store.rows("proxy_pool").clear()
    store.rows("proxy_pool").extend([
        _node(1, "节点甲", "可用"),
        _node(2, "节点乙", "可用"),
        _node(3, "节点丙", "已剔除"),
    ])
    store.rows("error_logger").clear()
    yield


@pytest.fixture()
def svc() -> ProxyPoolService:
    return ProxyPoolService()


class TestCooldownLifecycle:
    def test_cooldown_sets_status_and_server_timing(self, svc):
        entry = svc.domain_cooldown_node(1, now_ts=T0)
        assert entry["status"] == "已冷却"
        assert entry["冷却开始时间"] == T0
        assert entry["冷却到期时间"] == T0 + 30
        # 冷却配置 30 秒，节点自身配置优先
        assert entry["冷却秒数"] == 30

    def test_cooling_node_cannot_be_taken_before_expiry(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        result = svc.domain_allocate_proxy(trace_id="TRACE-T1", now_ts=T0 + 29)
        # 甲在冷却中，实际取用乙
        assert result["allocated"] is True
        assert result["selected_node_id"] == 2
        jia = next(j for j in result["judgments"] if j["node_id"] == 1)
        assert jia["assignable"] is False
        assert jia["remaining_seconds"] == 1
        assert "冷却中" in jia["reason"]

    def test_judgment_records_reflect_server_start_time(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        records = svc.domain_node_judgments(now_ts=T0 + 10)
        jia = next(r for r in records if r["node_id"] == 1)
        assert jia["冷却开始时间"] == T0
        assert jia["elapsed_seconds"] == 10
        assert jia["remaining_seconds"] == 20
        assert jia["expired"] is False
        assert jia["assignable"] is False

    def test_removed_node_keeps_original_status_forever(self, svc):
        svc.domain_node_judgments(now_ts=T0 + 999)
        bing = store.find("proxy_pool", 3)
        assert bing["status"] == "已剔除"
        result = svc.domain_allocate_proxy(now_ts=T0 + 999)
        record = next(j for j in result["judgments"] if j["node_id"] == 3)
        assert record["assignable"] is False
        assert "已剔除" in record["reason"]
        with pytest.raises(ValueError):
            svc.domain_cooldown_node(3, now_ts=T0)
        with pytest.raises(ValueError):
            svc.domain_submit_recheck(3, True, now_ts=T0)

    def test_independent_timers_for_jia_and_yi(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)          # 甲 T0 起冷
        svc.domain_cooldown_node(2, now_ts=T0 + 15)     # 乙晚 15 秒，分别计时
        records = {r["node_id"]: r for r in svc.domain_node_judgments(now_ts=T0 + 20)}
        assert records[1]["elapsed_seconds"] == 20
        assert records[1]["remaining_seconds"] == 10
        assert records[2]["elapsed_seconds"] == 5
        assert records[2]["remaining_seconds"] == 25


class TestExpiryRecheckGate:
    def test_29s_30s_checks_then_recheck_three_returns(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)

        # 第 29 秒：甲仍在冷却，取得乙
        r29 = svc.domain_allocate_proxy(
            trace_id="TRACE-29", recovery_mode=RECOVERY_RECHECK, now_ts=T0 + 29)
        assert r29["selected_node_id"] == 2
        assert store.find("proxy_pool", 1)["status"] == "已冷却"

        # 第 30 秒（复测门禁）：冷却期满但复测结果迟到，甲转待检测，仍取得乙
        r30 = svc.domain_allocate_proxy(
            trace_id="TRACE-30", recovery_mode=RECOVERY_RECHECK, now_ts=T0 + 30)
        assert r30["selected_node_id"] == 2
        jia_judgment = next(j for j in r30["judgments"] if j["node_id"] == 1)
        assert jia_judgment["status"] == "待检测"
        assert jia_judgment["assignable"] is False
        assert "等待复测" in jia_judgment["reason"]
        assert store.find("proxy_pool", 1)["status"] == "待检测"

        # 补甲的复测结果：通过后恢复可分配，下一次检查取得甲
        svc.domain_submit_recheck(1, True, now_ts=T0 + 31)
        assert store.find("proxy_pool", 1)["status"] == "可用"
        r31 = svc.domain_allocate_proxy(trace_id="TRACE-31", now_ts=T0 + 32)
        assert r31["selected_node_id"] == 1

    def test_expiry_immediate_mode_restores_and_allocates_jia(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        result = svc.domain_allocate_proxy(
            trace_id="TRACE-IMM",
            recovery_mode=RECOVERY_IMMEDIATE,
            now_ts=T0 + 30,
        )
        # 甲是优先候选（id 在前），期满立即恢复并分配甲
        assert result["selected_node_id"] == 1
        assert result["recovery_mode_text"] == "期满立即恢复"
        jia = store.find("proxy_pool", 1)
        assert jia["status"] == "可用"
        assert jia["恢复时间"] == T0 + 30
        judgment = next(j for j in result["judgments"] if j["node_id"] == 1)
        assert judgment["assignable"] is True

    def test_detail_reflects_which_status_was_used(self, svc):
        # 同一时点、两种恢复模式各自独立演示，详情反映本次实际采用的状态
        svc.domain_cooldown_node(1, now_ts=T0)
        recheck = svc.domain_allocate_proxy(
            trace_id="A", recovery_mode=RECOVERY_RECHECK, now_ts=T0 + 30)
        assert recheck["recovery_mode"] == RECOVERY_RECHECK
        assert next(j for j in recheck["judgments"] if j["node_id"] == 1)["status"] == "待检测"

        # 重置甲，再用立即恢复模式演示同一期满时点
        store.rows("proxy_pool")[0].update(_node(1, "节点甲", "可用"))
        svc.domain_cooldown_node(1, now_ts=T0)
        immediate = svc.domain_allocate_proxy(
            trace_id="B", recovery_mode=RECOVERY_IMMEDIATE, now_ts=T0 + 30)
        assert immediate["recovery_mode"] == RECOVERY_IMMEDIATE
        assert next(j for j in immediate["judgments"] if j["node_id"] == 1)["status"] == "可用"

    def test_failed_recheck_keeps_pending(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        svc.domain_node_judgments(now_ts=T0 + 30)  # 期满转待检测
        svc.domain_submit_recheck(1, False, now_ts=T0 + 31)
        assert store.find("proxy_pool", 1)["status"] == "待检测"
        result = svc.domain_allocate_proxy(trace_id="C", now_ts=T0 + 32)
        assert result["selected_node_id"] == 2


class TestNoNodeAndTraceRecovery:
    def test_no_node_response_logs_trace_and_exclusion_details(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        svc.domain_cooldown_node(2, now_ts=T0 + 5)
        result = svc.domain_allocate_proxy(
            trace_id="TRACE-EMPTY",
            recovery_mode=RECOVERY_RECHECK,
            now_ts=T0 + 10,
        )
        assert result["allocated"] is False
        assert result["proxy"] is None
        assert result["selected_node_id"] is None

        log = result["error_log"]
        assert log is not None
        assert log["追踪号"] == "TRACE-EMPTY"
        assert log["错误码"] == "ERR-NO-PROXY"
        details = json.loads(log["排除明细"])
        # 排除明细覆盖甲、乙（冷却中）和丙（已剔除），各自给出依据
        by_node = {item["节点标识"]: item for item in details}
        assert "冷却中" in by_node["节点甲"]["reason"]
        assert "冷却中" in by_node["节点乙"]["reason"]
        assert "已剔除" in by_node["节点丙"]["reason"]
        for item in details:
            assert item["冷却开始时间文本"] or item["status"] == "已剔除"

    def test_filter_error_list_by_trace_after_recovery(self, svc):
        svc.domain_cooldown_node(1, now_ts=T0)
        svc.domain_cooldown_node(2, now_ts=T0 + 5)
        empty = svc.domain_allocate_proxy(trace_id="TRACE-RECOVER", now_ts=T0 + 10)
        assert empty["allocated"] is False

        # 恢复甲（复测通过）后，从错误列表按原追踪号仍能打开原排除明细
        svc.domain_node_judgments(now_ts=T0 + 30)
        svc.domain_submit_recheck(1, True, now_ts=T0 + 31)
        traced = ErrorLoggerService().domain_find_by_trace("TRACE-RECOVER")
        assert len(traced) == 1
        details = json.loads(traced[0]["排除明细"])
        assert {item["节点标识"] for item in details} == {"节点甲", "节点乙", "节点丙"}

        # 恢复后正常检查可以再次分配
        again = svc.domain_allocate_proxy(trace_id="TRACE-OK", now_ts=T0 + 32)
        assert again["allocated"] is True
        assert again["selected_node_id"] == 1
        assert again["error_log"] is None

    def test_generic_list_filter_by_trace_field(self):
        # 值班员从错误列表页用「追踪号」字段筛选
        rows, total = ErrorLoggerService().list_entries(
            filters={"追踪号": "TRACE-X"}, page=1, size=20)
        assert total == 0
        ErrorLoggerService().domain_log_error(
            "ERR-NO-PROXY", "无可用节点", "proxy_pool", trace_id="TRACE-X")
        rows, total = ErrorLoggerService().list_entries(
            filters={"追踪号": "TRACE-X"}, page=1, size=20)
        assert total == 1
        assert rows[0]["追踪号"] == "TRACE-X"


class TestReliabilityChainConsumesJudgments:
    def test_chain_uses_cooldown_judgments_and_trace(self):
        svc = ProxyPoolService()
        svc.domain_cooldown_node(1, now_ts=T0)
        result = run_reliability({
            "site": "example.com",
            "protocol": "http",
            "trace_id": "TRACE-CHAIN",
            "recovery_mode": RECOVERY_RECHECK,
            "now_ts": T0 + 29,
        })
        assert result["chain"] == "reliability"
        assert result["trace_id"] == "TRACE-CHAIN"
        allocate = result["steps"]["allocate_proxy"]
        assert allocate["selected_node_id"] == 2
        assert allocate["judgments"], "可靠性响应必须包含节点冷却判定记录"
        assert result["steps"]["log_error"]["追踪号"] == "TRACE-CHAIN"

    def test_chain_no_node_carries_exclusion_detail(self):
        svc = ProxyPoolService()
        svc.domain_cooldown_node(1, now_ts=T0)
        svc.domain_cooldown_node(2, now_ts=T0 + 5)
        result = run_reliability({
            "site": "example.com",
            "trace_id": "TRACE-CHAIN-EMPTY",
            "now_ts": T0 + 10,
        })
        assert result["steps"]["allocate_proxy"]["allocated"] is False
        log = result["steps"]["log_error"]
        assert log["error_code"] == "ERR-NO-PROXY"
        assert log["追踪号"] == "TRACE-CHAIN-EMPTY"
        assert json.loads(log["排除明细"])
