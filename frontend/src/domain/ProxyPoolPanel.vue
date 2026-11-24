<template>
  <section class="proxy-panel" data-testid="proxy-pool-panel">
    <header class="panel-head">
      <h3>代理节点冷却调度台</h3>
      <p class="panel-desc">
        冷却判定以服务端登记的冷却开始时间和当前节点状态为准；已剔除节点保持原状态，不参与分配。
      </p>
    </header>

    <div class="panel-controls">
      <label class="mode-item">
        <span>期满恢复策略</span>
        <select v-model="recoveryMode" data-testid="recovery-mode" @change="loadJudgments">
          <option value="recheck">复测门禁：期满转待检测，复测通过才恢复</option>
          <option value="immediate">立即恢复：期满即可恢复并直接分配</option>
        </select>
      </label>
      <button class="btn" type="button" data-testid="refresh-judgments" @click="loadJudgments">
        刷新冷却判定
      </button>
      <button class="btn primary" type="button" data-testid="run-allocate" @click="runAllocate">
        发起检查分配
      </button>
    </div>

    <p v-if="message" class="panel-message" :class="messageKind" data-testid="panel-message">
      {{ message }}
    </p>

    <h4 class="block-title">节点冷却判定记录</h4>
    <table class="data-table judgment-table">
      <thead>
        <tr>
          <th>节点标识</th>
          <th>当前状态</th>
          <th>冷却开始时间（服务端）</th>
          <th>冷却到期时间</th>
          <th>剩余/已冷却</th>
          <th>复测状态</th>
          <th>判定依据</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in judgments" :key="item.node_id" :data-testid="`judgment-${item.node_id}`">
          <td>
            <button class="link" type="button" @click="openDetail(item)">{{ item.节点标识 }}</button>
          </td>
          <td>
            <span class="status-tag" :class="`tag-${item.status}`">{{ item.status }}</span>
          </td>
          <td>{{ item.冷却开始时间文本 || '—' }}</td>
          <td>{{ item.冷却到期时间文本 || '—' }}</td>
          <td>
            <template v-if="item.冷却开始时间">
              已冷却 {{ item.elapsed_seconds }}s / 剩余 {{ item.remaining_seconds }}s
            </template>
            <span v-else>—</span>
          </td>
          <td>{{ item.复测状态 || '—' }}</td>
          <td class="reason-cell">{{ item.reason }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`cooldown-${item.node_id}`"
              @click="handleCooldown(item.node_id)"
            >
              冷却节点
            </button>
            <button
              v-if="item.status === '待检测'"
              class="link"
              type="button"
              :data-testid="`recheck-pass-${item.node_id}`"
              @click="handleRecheck(item.node_id, true)"
            >
              复测通过
            </button>
            <button
              v-if="item.status === '待检测'"
              class="link"
              type="button"
              :data-testid="`recheck-fail-${item.node_id}`"
              @click="handleRecheck(item.node_id, false)"
            >
              复测未过
            </button>
          </td>
        </tr>
        <tr v-if="!judgments.length">
          <td colspan="8" class="empty-state">暂无节点判定记录</td>
        </tr>
      </tbody>
    </table>

    <div v-if="lastAllocation" class="allocation-box" data-testid="allocation-result">
      <h4 class="block-title">最近一次检查响应</h4>
      <dl class="alloc-grid">
        <dt>本次追踪号</dt>
        <dd data-testid="alloc-trace">{{ lastAllocation.trace_id }}</dd>
        <dt>实际取用节点</dt>
        <dd>
          <span v-if="lastAllocation.allocated">
            节点 #{{ lastAllocation.selected_node_id }}（{{ lastAllocation.recovery_mode_text }}）
          </span>
          <span v-else class="error-text">无可用节点</span>
        </dd>
        <dt>服务端计时</dt>
        <dd>{{ lastAllocation.server_time }}</dd>
      </dl>
      <table class="data-table">
        <thead>
          <tr><th>节点</th><th>采用状态</th><th>是否可分配</th><th>判定依据</th></tr>
        </thead>
        <tbody>
          <tr v-for="j in lastAllocation.judgments" :key="j.node_id">
            <td>{{ j.节点标识 }}</td>
            <td>{{ j.status }}</td>
            <td>{{ j.assignable ? '是' : '否' }}</td>
            <td class="reason-cell">{{ j.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="trace-box">
      <h4 class="block-title">按追踪号排查无节点错误</h4>
      <form class="trace-form" @submit.prevent="lookupTrace">
        <input
          v-model="traceInput"
          data-testid="trace-input"
          placeholder="输入当时的追踪号，例如 TRACE-20261124-xxxxxxxx"
        />
        <button class="btn" type="submit" data-testid="trace-search">打开原排除明细</button>
      </form>
      <div v-if="traceErrors.length" class="trace-result" data-testid="trace-result">
        <article v-for="err in traceErrors" :key="String(err.id)" class="error-card">
          <p>
            <strong>{{ err.错误码 }}</strong>
            <span class="muted">{{ err.发生时间 }} · 追踪号 {{ err.追踪号 }}</span>
          </p>
          <p>{{ err.错误信息 }}</p>
          <table v-if="parseDetails(err.排除明细)" class="data-table">
            <thead>
              <tr><th>节点</th><th>当时状态</th><th>冷却/复测依据</th></tr>
            </thead>
            <tbody>
              <tr v-for="d in parseDetails(err.排除明细)" :key="d.node_id">
                <td>{{ d.节点标识 }}</td>
                <td>{{ d.status }}</td>
                <td class="reason-cell">{{ d.reason }}</td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>
      <p v-else-if="traceSearched" class="panel-message" data-testid="trace-empty">
        未找到该追踪号的错误记录
      </p>
    </div>

    <div v-if="detailNode" class="modal-backdrop" data-testid="node-detail-modal" @click.self="detailNode = null">
      <div class="modal">
        <h3>节点详情：{{ detailNode.节点标识 }}</h3>
        <dl class="detail-grid">
          <dt>节点ID</dt><dd>{{ detailNode.node_id }}</dd>
          <dt>代理地址</dt><dd>{{ detailNode.代理地址 }}</dd>
          <dt>代理协议</dt><dd>{{ detailNode.代理协议 }}</dd>
          <dt>所属区域</dt><dd>{{ detailNode.所属区域 }}</dd>
          <dt>当前状态</dt><dd>{{ detailNode.status }}</dd>
          <dt>冷却配置</dt><dd>{{ detailNode.冷却秒数 }} 秒</dd>
          <dt>冷却开始时间</dt><dd>{{ detailNode.冷却开始时间文本 || '—' }}</dd>
          <dt>冷却到期时间</dt><dd>{{ detailNode.冷却到期时间文本 || '—' }}</dd>
          <dt>已冷却 / 剩余</dt>
          <dd>{{ detailNode.elapsed_seconds }}s / {{ detailNode.remaining_seconds }}s</dd>
          <dt>复测状态</dt><dd>{{ detailNode.复测状态 || '—' }}</dd>
          <dt>恢复时间</dt><dd>{{ formatStamp(detailNode.恢复时间) }}</dd>
          <dt>判定依据</dt><dd class="reason-cell">{{ detailNode.reason }}</dd>
        </dl>
        <div class="page-actions">
          <button class="btn primary" type="button" @click="detailNode = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  allocateProxy,
  cooldownNode,
  fetchNodeJudgments,
  findErrorsByTrace,
  submitRecheck,
  type AllocateResult,
  type ExclusionDetail,
  type NodeJudgment,
  type TraceError,
} from '@/api/proxy-pool'

const emit = defineEmits<{ (event: 'data-changed'): void }>()

const recoveryMode = ref('recheck')
const judgments = ref<NodeJudgment[]>([])
const lastAllocation = ref<AllocateResult | null>(null)
const detailNode = ref<NodeJudgment | null>(null)
const message = ref('')
const messageKind = ref('')
const traceInput = ref('')
const traceErrors = ref<TraceError[]>([])
const traceSearched = ref(false)

function flash(text: string, kind: 'ok' | 'err' = 'ok') {
  message.value = text
  messageKind.value = kind
}

function formatStamp(stamp: number | null): string {
  if (!stamp) return '—'
  return new Date(stamp * 1000).toLocaleString('zh-CN', { hour12: false })
}

function parseDetails(raw: string | undefined): ExclusionDetail[] | null {
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as ExclusionDetail[]
    return Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

async function loadJudgments() {
  try {
    const payload = await fetchNodeJudgments(recoveryMode.value)
    judgments.value = payload.items
  } catch (error) {
    flash(error instanceof Error ? error.message : '冷却判定记录读取失败', 'err')
  }
}

async function handleCooldown(nodeId: number) {
  try {
    await cooldownNode(nodeId)
    flash(`节点 ${nodeId} 已进入冷却，服务端已登记冷却开始时间`)
    await loadJudgments()
    emit('data-changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '冷却操作失败', 'err')
  }
}

async function handleRecheck(nodeId: number, passed: boolean) {
  try {
    const result = await submitRecheck(nodeId, passed)
    flash(result.message)
    await loadJudgments()
    emit('data-changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '复测结果提交失败', 'err')
  }
}

async function runAllocate() {
  try {
    lastAllocation.value = await allocateProxy(recoveryMode.value)
    if (lastAllocation.value.allocated) {
      flash(
        `本次检查取得节点 #${lastAllocation.value.selected_node_id}，追踪号 ${lastAllocation.value.trace_id}`,
      )
    } else {
      flash(
        `本次检查无可用节点，错误已按追踪号 ${lastAllocation.value.trace_id} 记录，含逐节点排除明细`,
        'err',
      )
    }
    await loadJudgments()
    emit('data-changed')
  } catch (error) {
    flash(error instanceof Error ? error.message : '检查分配失败', 'err')
  }
}

async function lookupTrace() {
  traceSearched.value = true
  traceErrors.value = []
  const trace = traceInput.value.trim()
  if (!trace) {
    flash('请输入追踪号', 'err')
    return
  }
  try {
    const payload = await findErrorsByTrace(trace)
    traceErrors.value = payload.items
  } catch (error) {
    flash(error instanceof Error ? error.message : '追踪号查询失败', 'err')
  }
}

function openDetail(node: NodeJudgment) {
  detailNode.value = node
}

onMounted(loadJudgments)
</script>

<style scoped>
.proxy-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 14px 0;
}
.panel-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.panel-desc {
  margin: 0 0 12px;
  color: var(--muted);
  font-size: 12px;
}
.panel-controls {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
}
.mode-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.mode-item select {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  min-width: 320px;
}
.block-title {
  font-size: 13px;
  margin: 16px 0 8px;
}
.judgment-table td,
.judgment-table th {
  font-size: 12px;
}
.reason-cell {
  color: #475569;
  max-width: 260px;
}
.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  background: #e2e8f0;
}
.tag-可用 {
  background: #dcfce7;
  color: #166534;
}
.tag-已冷却 {
  background: #fef9c3;
  color: #854d0e;
}
.tag-待检测 {
  background: #e0e7ff;
  color: #3730a3;
}
.tag-已剔除 {
  background: #fee2e2;
  color: #991b1b;
}
.panel-message {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: #ecfdf5;
  color: #065f46;
  font-size: 12px;
}
.panel-message.err {
  background: #fef2f2;
  color: #991b1b;
}
.alloc-grid,
.detail-grid {
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: 6px 12px;
  margin: 0 0 10px;
  font-size: 13px;
}
.alloc-grid dt,
.detail-grid dt {
  color: var(--muted);
  font-size: 12px;
}
.alloc-grid dd,
.detail-grid dd {
  margin: 0;
}
.trace-form {
  display: flex;
  gap: 8px;
}
.trace-form input {
  flex: 1;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.error-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-top: 10px;
}
.error-card p {
  margin: 4px 0;
  font-size: 13px;
}
.muted {
  color: var(--muted);
  font-size: 12px;
  margin-left: 8px;
}
</style>
