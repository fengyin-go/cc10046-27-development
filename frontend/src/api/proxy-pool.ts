import { request } from '@/api/client'

/** 节点冷却判定记录：与后端 ProxyPoolService.domain_node_judgments 对齐。 */
export type NodeJudgment = {
  node_id: number
  节点标识: string
  代理地址: string
  代理协议: string
  所属区域: string
  status: string
  assignable: boolean
  reason: string
  冷却秒数: number
  冷却开始时间: number | null
  冷却开始时间文本: string
  冷却到期时间: number | null
  冷却到期时间文本: string
  elapsed_seconds: number
  remaining_seconds: number
  expired: boolean
  复测状态: string
  复测结果时间: number | null
  恢复时间: number | null
  recovery_mode: string
}

export type ExclusionDetail = {
  node_id: number
  节点标识: string
  status: string
  reason: string
  冷却开始时间: number | null
  冷却开始时间文本: string
  冷却到期时间: number | null
  冷却到期时间文本: string
  remaining_seconds: number
  复测状态: string
}

export type AllocateResult = {
  allocated: boolean
  proxy: Record<string, unknown> | null
  candidate_count: number
  protocol: string
  region: string
  trace_id: string
  recovery_mode: string
  recovery_mode_text: string
  server_time: string
  now_ts: number
  selected_node_id: number | null
  judgments: NodeJudgment[]
  excluded: ExclusionDetail[]
  error_log: Record<string, unknown> | null
}

export type TraceError = {
  id: number
  错误码: string
  错误信息: string
  来源模块: string
  发生时间: string
  追踪号: string
  严重级别: string
  处理状态: string
  排除明细?: string
  [field: string]: unknown
}

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(detail.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

/** 读取采集节点状态数据，返回节点冷却判定记录。 */
export async function fetchNodeJudgments(
  recoveryMode: string,
): Promise<{ items: NodeJudgment[]; recovery_mode: string }> {
  const response = await request(`/api/proxy-pool/cooldown/judgments?recovery_mode=${recoveryMode}`)
  if (!response.ok) throw new Error('节点冷却判定记录读取失败')
  return await response.json()
}

/** 执行冷却：节点进入已冷却，服务端登记冷却开始时间。 */
export async function cooldownNode(
  nodeId: number,
  cooldownSeconds = 30,
): Promise<{ ok: boolean; message: string; entry: Record<string, unknown> }> {
  return postJson(`/api/proxy-pool/${nodeId}/cooldown`, { cooldown_seconds: cooldownSeconds })
}

/** 补录复测结果：通过恢复可用，未通过继续待检测。 */
export async function submitRecheck(
  nodeId: number,
  passed: boolean,
): Promise<{ ok: boolean; message: string; entry: Record<string, unknown> }> {
  return postJson(`/api/proxy-pool/${nodeId}/recheck`, { passed })
}

/** 发起一次检查分配，返回实际取用节点、判定记录、追踪号和排除明细。 */
export async function allocateProxy(
  recoveryMode: string,
  traceId = '',
): Promise<AllocateResult> {
  return postJson('/api/proxy-pool/allocate', {
    protocol: 'http',
    recovery_mode: recoveryMode,
    trace_id: traceId,
  })
}

/** 按追踪号从错误记录打开当时的排除明细。 */
export async function findErrorsByTrace(traceId: string): Promise<{ items: TraceError[]; total: number }> {
  const response = await request(`/api/proxy-pool/errors/${encodeURIComponent(traceId)}`)
  if (!response.ok) throw new Error('追踪号错误记录读取失败')
  return await response.json()
}
