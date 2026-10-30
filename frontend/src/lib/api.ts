import type {
  CreateTaskResp,
  DashboardStats,
  EvidenceQueryResp,
  Expert,
  ExpertWorkload,
  Report,
  ReportCard,
  ReportSection,
  SSEEventType,
  Subscription,
  SubscriptionDraft,
  SubscriptionTarget,
  MonitorRun,
  CompetitorChange,
  ChangeStatus,
  TargetCandidate,
  MonitorResearchTask,
  TraceSpan,
} from '../types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function safeJson<T>(path: string, init?: RequestInit, fallback?: T): Promise<T> {
  try {
    const r = await fetch(`${API_BASE}${path}`, init)
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return (await r.json()) as T
  } catch (e) {
    if (fallback !== undefined) return fallback
    throw e
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  if (!response.ok) {
    let message = `HTTP ${response.status}`
    try {
      const payload = await response.json() as { detail?: string }
      if (payload.detail) message = payload.detail
    } catch {
      // Keep the HTTP status when the response is not JSON.
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}

/* 48 专家：优先后端，失败回退本地 JSON（绝不白屏） */
export async function fetchExperts(): Promise<Expert[]> {
  try {
    const r = await fetch(`${API_BASE}/api/experts`)
    if (r.ok) {
      const data = await r.json()
      if (Array.isArray(data) && data.length) return data
      if (data?.experts?.length) return data.experts
    }
  } catch {
    /* fall through */
  }
  const local = await fetch('/assets/experts.json')
  return (await local.json()) as Expert[]
}

export async function createTask(query: string, mode: string = 'deep'): Promise<CreateTaskResp> {
  return safeJson<CreateTaskResp>(
    '/api/tasks',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, mode }),
    },
    { taskId: `demo-${Date.now()}`, needClarify: false },
  )
}

export async function submitClarify(
  taskId: string,
  answers: Record<string, unknown>,
): Promise<{ ok: boolean }> {
  return safeJson(
    `/api/tasks/${taskId}/clarify`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    },
    { ok: true },
  )
}

export async function fetchReport(reportId: string): Promise<Report | null> {
  return safeJson<Report | null>(`/api/reports/${reportId}`, undefined, null)
}

/* 报告决策链路 Trace（决策回放 / Trace 页签） */
export async function fetchReportTrace(reportId: string): Promise<{ spans: TraceSpan[] }> {
  return safeJson<{ spans: TraceSpan[] }>(`/api/reports/${reportId}/trace`, undefined, { spans: [] })
}

/* 人工反馈（修正率 → 业务闭环指标） */
export async function submitFeedback(
  reportId: string,
  editedBlocks: number,
  totalBlocks: number,
  data: Record<string, unknown> = {},
): Promise<{ ok: boolean }> {
  return safeJson(
    `/api/reports/${reportId}/feedback`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edited_blocks: editedBlocks, total_blocks: totalBlocks, data }),
    },
    { ok: true },
  )
}

/* 按批注深化章节（人工介入二次调研） */
export async function refineSection(
  reportId: string,
  sectionId: string,
  annotations: string[],
): Promise<{ ok: boolean; section?: ReportSection; message?: string }> {
  return safeJson(
    `/api/reports/${reportId}/refine`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section_id: sectionId, annotations }),
    },
    { ok: false, message: '请求失败' },
  )
}

/* 我的调研：真实历史报告列表 */
export async function fetchReports(): Promise<ReportCard[]> {
  return safeJson<ReportCard[]>('/api/reports', undefined, [])
}

/* 仪表盘真实统计 */
export async function fetchDashboard(): Promise<DashboardStats | null> {
  return safeJson<DashboardStats | null>('/api/dashboard', undefined, null)
}

/* 全局证据溯源库 */
export async function fetchEvidences(params?: {
  brand?: string
  source_type?: string
  min_cred?: number
}): Promise<EvidenceQueryResp> {
  const qs = new URLSearchParams()
  if (params?.brand) qs.set('brand', params.brand)
  if (params?.source_type) qs.set('source_type', params.source_type)
  if (params?.min_cred != null) qs.set('min_cred', String(params.min_cred))
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return safeJson<EvidenceQueryResp>(`/api/evidences${suffix}`, undefined, {
    items: [],
    facets: { total: 0, by_type: {}, by_brand: {} },
  })
}

/* 竞品监控订阅 */
export async function fetchSubscriptions(): Promise<Subscription[]> {
  return safeJson<Subscription[]>('/api/subscriptions', undefined, [])
}

export async function createSubscription(input: SubscriptionDraft): Promise<Subscription> {
  return requestJson<Subscription>(
    '/api/subscriptions',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    },
  )
}

export async function deleteSubscription(subId: string): Promise<{ ok: boolean }> {
  return requestJson(`/api/subscriptions/${subId}`, { method: 'DELETE' })
}

export async function updateSubscription(
  subId: string,
  input: Partial<Pick<SubscriptionDraft, 'query' | 'brands' | 'timezone' | 'schedule'>>,
): Promise<Subscription> {
  return requestJson(`/api/subscriptions/${subId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export async function pauseSubscription(subId: string): Promise<Subscription> {
  return requestJson(`/api/subscriptions/${subId}/pause`, { method: 'POST' })
}

export async function resumeSubscription(subId: string): Promise<Subscription> {
  return requestJson(`/api/subscriptions/${subId}/resume`, { method: 'POST' })
}

export async function runSubscription(
  subId: string,
): Promise<{ ok: boolean; run_id: string; status: string }> {
  return requestJson(`/api/subscriptions/${subId}/run`, { method: 'POST' })
}

export async function fetchSubscriptionTargets(subId: string): Promise<SubscriptionTarget[]> {
  return requestJson(`/api/subscriptions/${subId}/targets`)
}

export async function createSubscriptionTarget(
  subId: string,
  input: Pick<SubscriptionTarget, 'brand' | 'target_type' | 'url'> & { enabled?: boolean },
): Promise<SubscriptionTarget> {
  return requestJson(`/api/subscriptions/${subId}/targets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export async function updateSubscriptionTarget(
  subId: string,
  targetId: string,
  input: Partial<Pick<SubscriptionTarget, 'brand' | 'target_type' | 'url' | 'enabled'>>,
): Promise<SubscriptionTarget> {
  return requestJson(`/api/subscriptions/${subId}/targets/${targetId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })
}

export async function deleteSubscriptionTarget(
  subId: string,
  targetId: string,
): Promise<{ ok: boolean }> {
  return requestJson(`/api/subscriptions/${subId}/targets/${targetId}`, { method: 'DELETE' })
}

export async function fetchMonitorRuns(subId: string): Promise<MonitorRun[]> {
  return requestJson(`/api/subscriptions/${subId}/runs`)
}

export async function fetchCompetitorChanges(subId: string): Promise<CompetitorChange[]> {
  return requestJson(`/api/subscriptions/${subId}/changes`)
}

export async function updateCompetitorChange(
  changeId: string,
  changeStatus: ChangeStatus,
): Promise<CompetitorChange> {
  return requestJson(`/api/competitor-changes/${changeId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: changeStatus }),
  })
}

export async function createResearchFromChange(
  changeId: string,
  mode: 'quick' | 'deep' | 'expert' = 'deep',
): Promise<MonitorResearchTask> {
  return requestJson(`/api/competitor-changes/${changeId}/research`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
}

export async function discoverSubscriptionTargets(
  query: string,
  brands: string[],
): Promise<TargetCandidate[]> {
  const response = await requestJson<{ items: TargetCandidate[] }>(
    '/api/subscriptions/targets/discover',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, brands, limit_per_brand: 5 }),
    },
  )
  return response.items
}

/* 专家工作量看板 */
export async function fetchWorkload(): Promise<ExpertWorkload[]> {
  return safeJson<ExpertWorkload[]>('/api/experts/workload', undefined, [])
}

/* SSE：监听任务流，返回关闭函数 */
export interface SSEHandlers {
  onEvent: (type: SSEEventType, data: unknown) => void
  onError?: (e: unknown) => void
  onOpen?: () => void
}

export function openTaskStream(taskId: string, handlers: SSEHandlers): () => void {
  const url = `${API_BASE}/api/tasks/${taskId}/stream`
  const es = new EventSource(url)
  const types: SSEEventType[] = [
    'node_update',
    'thought',
    'message',
    'evidence',
    'chart',
    'image',
    'progress',
    'trace',
    'collect_agent_action',
    'collect_tool_call',
    'collect_stop_check',
    'collect_incomplete',
    'evidence_request',
    'post_write_audit',
    'section_rewrite',
    'report_ready',
    'done',
    'error',
  ]
  es.onopen = () => handlers.onOpen?.()
  for (const t of types) {
    es.addEventListener(t, (ev) => {
      let parsed: unknown = (ev as MessageEvent).data
      try {
        parsed = JSON.parse((ev as MessageEvent).data)
      } catch {
        /* keep raw */
      }
      handlers.onEvent(t, parsed)
    })
  }
  es.onerror = (e) => {
    handlers.onError?.(e)
  }
  return () => es.close()
}

export { API_BASE }
