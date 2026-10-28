import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  Bell,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  ExternalLink,
  FileDiff,
  LoaderCircle,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Target,
  Trash2,
  X,
} from 'lucide-react'
import {
  createSubscription,
  createResearchFromChange,
  createSubscriptionTarget,
  deleteSubscription,
  deleteSubscriptionTarget,
  discoverSubscriptionTargets,
  fetchCompetitorChanges,
  fetchMonitorRuns,
  fetchSubscriptions,
  pauseSubscription,
  resumeSubscription,
  runSubscription,
  updateCompetitorChange,
  updateSubscription,
  updateSubscriptionTarget,
} from '../lib/api'
import type {
  CompetitorChange,
  MonitorRun,
  MonitorTargetType,
  Subscription,
  TargetCandidate,
} from '../types'

type DetailTab = 'overview' | 'targets' | 'runs' | 'changes'

const TARGET_LABEL: Record<MonitorTargetType, string> = {
  official: '官网',
  pricing: '定价',
  blog: '动态',
  jobs: '招聘',
  custom: '自定义',
}

const RUN_LABEL: Record<string, string> = {
  pending: '等待执行',
  running: '执行中',
  succeeded: '成功',
  partial: '部分成功',
  failed: '失败',
}

function displayTime(value: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function statusTone(status: string) {
  if (status === 'succeeded' || status === 'active') return 'bg-ok/15 text-primary-deep'
  if (status === 'partial' || status === 'claimed') return 'bg-sun-soft text-warn'
  if (status === 'failed' || status === 'error') return 'bg-risk/10 text-risk'
  if (status === 'running') return 'bg-info/15 text-info'
  return 'bg-bg text-ink-3'
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '操作失败'
}

export default function MonitoringPage() {
  const navigate = useNavigate()
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [tab, setTab] = useState<DetailTab>('overview')
  const [runs, setRuns] = useState<MonitorRun[]>([])
  const [changes, setChanges] = useState<CompetitorChange[]>([])
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<'all' | 'active' | 'attention'>('all')
  const [editor, setEditor] = useState<Subscription | null | undefined>(undefined)

  const refresh = useCallback(async () => {
    try {
      const items = await fetchSubscriptions()
      setSubscriptions(items)
      setSelectedId((current) => (
        current && items.some((item) => item.sub_id === current)
          ? current
          : items[0]?.sub_id ?? ''
      ))
      setError('')
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setLoading(false)
    }
  }, [])

  const refreshDetail = useCallback(async (subId: string) => {
    if (!subId) {
      setRuns([])
      setChanges([])
      return
    }
    setDetailLoading(true)
    try {
      const [nextRuns, nextChanges] = await Promise.all([
        fetchMonitorRuns(subId),
        fetchCompetitorChanges(subId),
      ])
      setRuns(nextRuns)
      setChanges(nextChanges)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    void (async () => {
      await refresh()
    })()
    const timer = window.setInterval(() => void refresh(), 15_000)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    void (async () => {
      await refreshDetail(selectedId)
    })()
  }, [refreshDetail, selectedId])

  const selected = subscriptions.find((item) => item.sub_id === selectedId) ?? null
  const filtered = useMemo(() => subscriptions.filter((item) => {
    if (filter === 'active') return item.enabled
    if (filter === 'attention') {
      return item.last_status === 'failed'
        || item.last_status === 'partial'
        || item.last_status === 'needs_configuration'
        || item.consecutive_failures > 0
    }
    return true
  }), [subscriptions, filter])

  const metrics = useMemo(() => ({
    active: subscriptions.filter((item) => item.enabled).length,
    targets: subscriptions.reduce((sum, item) => sum + item.enabled_target_count, 0),
    changes: subscriptions.reduce((sum, item) => sum + item.unread_change_count, 0),
    attention: subscriptions.filter((item) => (
      item.consecutive_failures > 0 || ['failed', 'partial', 'needs_configuration'].includes(item.last_status)
    )).length,
  }), [subscriptions])

  async function perform(key: string, action: () => Promise<unknown>) {
    setBusy(key)
    setError('')
    try {
      await action()
      await refresh()
      if (selectedId) await refreshDetail(selectedId)
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setBusy('')
    }
  }

  async function removeSubscription(item: Subscription) {
    if (!window.confirm(`删除订阅“${item.query}”及其全部监控历史？`)) return
    await perform(`delete:${item.sub_id}`, () => deleteSubscription(item.sub_id))
  }

  return (
    <div className="mx-auto min-h-full max-w-content px-6 py-7 lg:px-8">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-line pb-5">
        <div>
          <h1 className="font-serif text-h1 text-ink">竞品监控</h1>
          <div className="mt-1 flex items-center gap-2 text-aux text-ink-3">
            <span>{subscriptions.length} 个订阅</span>
            <span>·</span>
            <span>{metrics.targets} 个有效目标</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            title="刷新"
            onClick={() => void refresh()}
            className="grid h-10 w-10 place-items-center rounded-btn border border-line bg-card text-ink-2 hover:bg-primary-tint"
          >
            <RefreshCw size={17} />
          </button>
          <button
            onClick={() => setEditor(null)}
            className="inline-flex h-10 items-center gap-2 rounded-btn bg-primary px-4 text-aux font-medium text-white hover:bg-primary-deep"
          >
            <Plus size={17} /> 新建订阅
          </button>
        </div>
      </header>

      {error && (
        <div className="mt-4 flex items-center justify-between rounded-btn border border-risk/30 bg-risk/10 px-4 py-3 text-aux text-risk">
          <span className="inline-flex items-center gap-2"><CircleAlert size={16} />{error}</span>
          <button title="关闭" onClick={() => setError('')}><X size={15} /></button>
        </div>
      )}

      <section className="grid grid-cols-2 border-b border-line py-5 sm:grid-cols-4">
        <Metric icon={Activity} label="运行中订阅" value={metrics.active} />
        <Metric icon={Target} label="有效目标" value={metrics.targets} />
        <Metric icon={Bell} label="未读变化" value={metrics.changes} />
        <Metric icon={CircleAlert} label="需要处理" value={metrics.attention} />
      </section>

      <div className="grid min-h-[640px] lg:grid-cols-[340px_minmax(0,1fr)]">
        <aside className="border-b border-line py-5 lg:border-b-0 lg:border-r lg:pr-5">
          <div className="flex gap-1">
            {([
              ['all', '全部'],
              ['active', '运行中'],
              ['attention', '需处理'],
            ] as const).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setFilter(key)}
                className={`h-8 rounded-btn px-3 text-tag font-medium ${
                  filter === key ? 'bg-primary text-white' : 'text-ink-2 hover:bg-primary-tint'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="mt-4 space-y-2">
            {loading ? (
              <div className="flex items-center justify-center py-16 text-ink-3">
                <LoaderCircle className="animate-spin" size={20} />
              </div>
            ) : filtered.length === 0 ? (
              <div className="border border-dashed border-line px-4 py-12 text-center text-aux text-ink-3">
                暂无订阅
              </div>
            ) : filtered.map((item) => (
              <button
                key={item.sub_id}
                onClick={() => {
                  setSelectedId(item.sub_id)
                  setTab('overview')
                }}
                className={`w-full border p-3 text-left transition-colors ${
                  selectedId === item.sub_id
                    ? 'border-primary-soft bg-primary-tint/60'
                    : 'border-line/70 bg-card hover:border-primary-soft'
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <span className="line-clamp-2 text-aux font-medium text-ink">{item.query}</span>
                  {item.unread_change_count > 0 && (
                    <span className="grid h-5 min-w-5 place-items-center rounded-chip bg-risk px-1 text-tag text-white">
                      {item.unread_change_count}
                    </span>
                  )}
                </div>
                <div className="mt-2 flex flex-wrap gap-1">
                  {item.brands.slice(0, 3).map((brand) => (
                    <span key={brand} className="bg-bg px-1.5 py-0.5 text-tag text-ink-2">{brand}</span>
                  ))}
                </div>
                <div className="mt-3 flex items-center justify-between text-tag text-ink-3">
                  <span className={`px-2 py-0.5 ${statusTone(item.last_status)}`}>
                    {item.enabled ? '运行中' : item.last_status === 'needs_configuration' ? '待配置' : '已暂停'}
                  </span>
                  <span>{item.enabled_target_count} 个目标</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <main className="min-w-0 py-5 lg:pl-6">
          {!selected ? (
            <div className="grid min-h-[480px] place-items-center text-aux text-ink-3">
              选择一个订阅查看详情
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h2 className="truncate text-h2 text-ink">{selected.query}</h2>
                    <span className={`shrink-0 px-2 py-1 text-tag ${statusTone(selected.last_status)}`}>
                      {selected.enabled ? '运行中' : selected.last_status === 'needs_configuration' ? '待配置' : '已暂停'}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {selected.brands.map((brand) => (
                      <span key={brand} className="bg-primary-tint px-2 py-1 text-tag text-primary-deep">{brand}</span>
                    ))}
                  </div>
                </div>
                <div className="flex items-center gap-1.5">
                  <IconButton title="编辑" onClick={() => setEditor(selected)}><Pencil size={16} /></IconButton>
                  <IconButton
                    title={selected.enabled ? '暂停' : '恢复'}
                    disabled={busy !== ''}
                    onClick={() => void perform(
                      `toggle:${selected.sub_id}`,
                      () => selected.enabled ? pauseSubscription(selected.sub_id) : resumeSubscription(selected.sub_id),
                    )}
                  >
                    {busy === `toggle:${selected.sub_id}`
                      ? <LoaderCircle size={16} className="animate-spin" />
                      : selected.enabled ? <Pause size={16} /> : <Play size={16} />}
                  </IconButton>
                  <button
                    disabled={!selected.enabled || busy !== ''}
                    onClick={() => void perform(`run:${selected.sub_id}`, () => runSubscription(selected.sub_id))}
                    className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-primary px-3 text-tag font-medium text-white hover:bg-primary-deep disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {busy === `run:${selected.sub_id}`
                      ? <LoaderCircle size={15} className="animate-spin" />
                      : <RefreshCw size={15} />}
                    立即检查
                  </button>
                  <IconButton
                    title="删除"
                    disabled={busy !== ''}
                    danger
                    onClick={() => void removeSubscription(selected)}
                  >
                    <Trash2 size={16} />
                  </IconButton>
                </div>
              </div>

              <div className="mt-5 flex border-b border-line">
                {([
                  ['overview', '概览'],
                  ['targets', `目标 ${selected.target_count}`],
                  ['runs', `运行 ${runs.length}`],
                  ['changes', `变化 ${changes.length}`],
                ] as const).map(([key, label]) => (
                  <button
                    key={key}
                    onClick={() => setTab(key)}
                    className={`h-10 border-b-2 px-4 text-aux font-medium ${
                      tab === key
                        ? 'border-primary text-primary-deep'
                        : 'border-transparent text-ink-3 hover:text-ink'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>

              {detailLoading ? (
                <div className="grid min-h-[360px] place-items-center text-ink-3">
                  <LoaderCircle size={20} className="animate-spin" />
                </div>
              ) : (
                <div className="pt-5">
                  {tab === 'overview' && <Overview subscription={selected} runs={runs} />}
                  {tab === 'targets' && (
                    <TargetsPanel
                      subscription={selected}
                      busy={busy}
                      perform={perform}
                    />
                  )}
                  {tab === 'runs' && <RunsPanel runs={runs} />}
                  {tab === 'changes' && (
                    <ChangesPanel
                      changes={changes}
                      onResearch={async (change) => {
                        if (change.research_report_id) {
                          navigate(`/report/${change.research_report_id}`)
                          return
                        }
                        if (change.research_task_id) {
                          navigate(`/workspace/${change.research_task_id}`)
                          return
                        }
                        setBusy(`research:${change.change_id}`)
                        setError('')
                        try {
                          const task = await createResearchFromChange(change.change_id)
                          navigate(`/clarify/${task.taskId}`, {
                            state: {
                              query: task.query,
                              clarify: task.clarifyQuestions,
                              answers: task.prefill,
                            },
                          })
                        } catch (requestError) {
                          setError(errorMessage(requestError))
                          setBusy('')
                        }
                      }}
                      onStatus={async (change, nextStatus) => {
                        await perform(
                          `change:${change.change_id}`,
                          () => updateCompetitorChange(change.change_id, nextStatus),
                        )
                      }}
                    />
                  )}
                </div>
              )}
            </>
          )}
        </main>
      </div>

      {editor !== undefined && (
        <SubscriptionEditor
          subscription={editor}
          onClose={() => setEditor(undefined)}
          onSaved={async (subId) => {
            setEditor(undefined)
            await refresh()
            setSelectedId(subId)
          }}
        />
      )}
    </div>
  )
}

function Metric({ icon: Icon, label, value }: {
  icon: typeof Activity
  label: string
  value: number
}) {
  return (
    <div className="flex items-center gap-3 border-r border-line px-3 last:border-r-0 sm:px-5">
      <Icon size={18} className="shrink-0 text-primary" />
      <div>
        <div className="text-h2 text-ink">{value}</div>
        <div className="text-tag text-ink-3">{label}</div>
      </div>
    </div>
  )
}

function IconButton({ title, onClick, children, danger = false, disabled = false }: {
  title: string
  onClick: () => void
  children: React.ReactNode
  danger?: boolean
  disabled?: boolean
}) {
  return (
    <button
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`grid h-9 w-9 place-items-center rounded-btn border border-line bg-card disabled:opacity-40 ${
        danger ? 'text-ink-3 hover:border-risk/40 hover:text-risk' : 'text-ink-2 hover:bg-primary-tint'
      }`}
    >
      {children}
    </button>
  )
}

function Overview({ subscription, runs }: { subscription: Subscription; runs: MonitorRun[] }) {
  const schedule = subscription.schedule_type === 'daily'
    ? `每天 ${subscription.schedule_config.time ?? '09:00'}`
    : `每 ${subscription.schedule_config.interval_minutes ?? 1440} 分钟`
  return (
    <div>
      {subscription.monitor_error && (
        <div className="mb-5 border-l-4 border-risk bg-risk/10 p-4 text-aux text-risk">
          {subscription.monitor_error}
        </div>
      )}
      <dl className="grid grid-cols-2 gap-x-8 gap-y-5 border-b border-line pb-6 lg:grid-cols-4">
        <Info label="执行计划" value={schedule} icon={CalendarClock} />
        <Info label="下次检查" value={displayTime(subscription.next_run_at)} icon={Clock3} />
        <Info label="上次成功" value={displayTime(subscription.last_success_at)} icon={Check} />
        <Info label="连续失败" value={String(subscription.consecutive_failures)} icon={CircleAlert} />
      </dl>
      <h3 className="mt-6 text-aux font-semibold text-ink">最近运行</h3>
      <RunsPanel runs={runs.slice(0, 5)} compact />
    </div>
  )
}

function Info({ label, value, icon: Icon }: {
  label: string
  value: string
  icon: typeof Clock3
}) {
  return (
    <div>
      <dt className="flex items-center gap-1.5 text-tag text-ink-3"><Icon size={13} />{label}</dt>
      <dd className="mt-1.5 break-words text-aux font-medium text-ink">{value}</dd>
    </div>
  )
}

function TargetsPanel({ subscription, busy, perform }: {
  subscription: Subscription
  busy: string
  perform: (key: string, action: () => Promise<unknown>) => Promise<void>
}) {
  const [adding, setAdding] = useState(false)
  const [brand, setBrand] = useState(subscription.brands[0] ?? '')
  const [targetType, setTargetType] = useState<MonitorTargetType>('official')
  const [url, setUrl] = useState('')

  async function addTarget() {
    if (!brand.trim() || !url.trim()) return
    await perform('target:add', () => createSubscriptionTarget(subscription.sub_id, {
      brand: brand.trim(),
      target_type: targetType,
      url: url.trim(),
      enabled: true,
    }))
    setUrl('')
    setAdding(false)
  }

  return (
    <div>
      <div className="flex justify-end">
        <button
          onClick={() => setAdding((value) => !value)}
          className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-line px-3 text-tag font-medium text-ink-2 hover:bg-primary-tint"
        >
          {adding ? <X size={15} /> : <Plus size={15} />} {adding ? '取消' : '添加目标'}
        </button>
      </div>
      {adding && (
        <div className="mt-3 grid gap-2 border border-line bg-bg p-3 md:grid-cols-[140px_120px_minmax(0,1fr)_40px]">
          <input value={brand} onChange={(event) => setBrand(event.target.value)}
            placeholder="品牌" className="h-9 border border-line bg-card px-2 text-aux outline-none focus:border-primary" />
          <select value={targetType} onChange={(event) => setTargetType(event.target.value as MonitorTargetType)}
            className="h-9 border border-line bg-card px-2 text-aux outline-none focus:border-primary">
            {Object.entries(TARGET_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
          <input value={url} onChange={(event) => setUrl(event.target.value)}
            placeholder="https://..." className="h-9 border border-line bg-card px-2 text-aux outline-none focus:border-primary" />
          <button title="保存目标" onClick={() => void addTarget()}
            className="grid h-9 w-9 place-items-center bg-primary text-white disabled:opacity-40"
            disabled={!brand.trim() || !url.trim() || busy !== ''}>
            {busy === 'target:add' ? <LoaderCircle size={15} className="animate-spin" /> : <Check size={15} />}
          </button>
        </div>
      )}
      <div className="mt-4 overflow-x-auto border border-line">
        <table className="w-full min-w-[720px] text-left text-aux">
          <thead className="bg-bg text-tag text-ink-3">
            <tr><th className="px-3 py-2.5">品牌</th><th className="px-3 py-2.5">类型</th><th className="px-3 py-2.5">页面</th><th className="px-3 py-2.5">失败</th><th className="px-3 py-2.5">状态</th><th className="px-3 py-2.5"></th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {subscription.targets.map((target) => (
              <tr key={target.target_id}>
                <td className="px-3 py-3 font-medium text-ink">{target.brand}</td>
                <td className="px-3 py-3 text-ink-2">{TARGET_LABEL[target.target_type]}</td>
                <td className="max-w-[360px] px-3 py-3">
                  <a href={target.url} target="_blank" rel="noreferrer"
                    className="flex items-center gap-1 truncate text-primary-deep hover:underline">
                    <span className="truncate">{target.url}</span><ExternalLink size={12} className="shrink-0" />
                  </a>
                </td>
                <td className="px-3 py-3 text-ink-2">{target.failure_count}</td>
                <td className="px-3 py-3">
                  <button
                    title={target.enabled ? '停用目标' : '启用目标'}
                    disabled={busy !== ''}
                    onClick={() => void perform(
                      `target:${target.target_id}`,
                      () => updateSubscriptionTarget(
                        subscription.sub_id,
                        target.target_id,
                        { enabled: !target.enabled },
                      ),
                    )}
                    className={`px-2 py-1 text-tag ${target.enabled ? 'bg-ok/15 text-primary-deep' : 'bg-bg text-ink-3'}`}
                  >
                    {target.enabled ? '启用' : '停用'}
                  </button>
                </td>
                <td className="px-3 py-3 text-right">
                  <button title="删除目标" disabled={busy !== ''}
                    onClick={() => void perform(
                      `target-delete:${target.target_id}`,
                      () => deleteSubscriptionTarget(subscription.sub_id, target.target_id),
                    )}
                    className="text-ink-3 hover:text-risk"><Trash2 size={14} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {subscription.targets.length === 0 && <div className="py-10 text-center text-aux text-ink-3">暂无目标</div>}
      </div>
    </div>
  )
}

function RunsPanel({ runs, compact = false }: { runs: MonitorRun[]; compact?: boolean }) {
  return (
    <div className={compact ? 'mt-3' : ''}>
      <div className="overflow-x-auto border border-line">
        <table className="w-full min-w-[680px] text-left text-aux">
          <thead className="bg-bg text-tag text-ink-3">
            <tr><th className="px-3 py-2.5">状态</th><th className="px-3 py-2.5">触发</th><th className="px-3 py-2.5">开始</th><th className="px-3 py-2.5">目标</th><th className="px-3 py-2.5">变化</th><th className="px-3 py-2.5">Token</th><th className="px-3 py-2.5">耗时</th></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {runs.map((run) => (
              <tr key={run.run_id} title={run.error || undefined}>
                <td className="px-3 py-3"><span className={`px-2 py-1 text-tag ${statusTone(run.status)}`}>{RUN_LABEL[run.status] ?? run.status}</span></td>
                <td className="px-3 py-3 text-ink-2">{run.trigger_type === 'manual' ? '手动' : run.trigger_type === 'retry' ? '重试' : '定时'}</td>
                <td className="px-3 py-3 text-ink-2">{displayTime(run.started_at || run.created_at)}</td>
                <td className="px-3 py-3 text-ink-2">{run.success_count}/{run.target_count}</td>
                <td className="px-3 py-3 text-ink-2">{run.changed_count}</td>
                <td className="px-3 py-3 text-ink-2">{run.llm_tokens}</td>
                <td className="px-3 py-3 text-ink-2">{run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {runs.length === 0 && <div className="py-10 text-center text-aux text-ink-3">暂无运行记录</div>}
      </div>
    </div>
  )
}

function ChangesPanel({ changes, onStatus, onResearch }: {
  changes: CompetitorChange[]
  onStatus: (change: CompetitorChange, status: 'read' | 'ignored') => Promise<void>
  onResearch: (change: CompetitorChange) => Promise<void>
}) {
  const [expanded, setExpanded] = useState('')
  return (
    <div className="space-y-2">
      {changes.map((change) => {
        const open = expanded === change.change_id
        return (
          <div key={change.change_id} className="border border-line bg-card">
            <button
              onClick={() => setExpanded(open ? '' : change.change_id)}
              className="flex w-full items-start gap-3 p-4 text-left"
            >
              {open ? <ChevronDown size={16} className="mt-0.5 shrink-0 text-ink-3" /> : <ChevronRight size={16} className="mt-0.5 shrink-0 text-ink-3" />}
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-aux font-medium text-ink">{change.brand || '未知品牌'}</span>
                  <span className="bg-primary-tint px-2 py-0.5 text-tag text-primary-deep">{change.change_type}</span>
                  <span className={`px-2 py-0.5 text-tag ${
                    ['critical', 'high'].includes(change.severity) ? 'bg-risk/10 text-risk' : 'bg-bg text-ink-3'
                  }`}>{change.severity}</span>
                  {change.status === 'new' && <span className="h-2 w-2 rounded-full bg-risk" title="未读" />}
                </div>
                <p className="mt-1.5 line-clamp-2 text-aux text-ink-2">{change.diff_summary || '页面内容发生变化'}</p>
                <div className="mt-2 text-tag text-ink-3">{displayTime(change.created_at)} · 变化比例 {(change.change_ratio * 100).toFixed(1)}%</div>
              </div>
              <FileDiff size={17} className="mt-0.5 shrink-0 text-primary" />
            </button>
            {open && (
              <div className="border-t border-line bg-bg p-4">
                <div className="grid gap-4 xl:grid-cols-2">
                  <DiffBlock title="变化前" text={change.before_excerpt} tone="text-risk" />
                  <DiffBlock title="变化后" text={change.after_excerpt} tone="text-primary-deep" />
                </div>
                <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
                  <a href={change.source_url} target="_blank" rel="noreferrer"
                    className="inline-flex items-center gap-1 text-tag text-primary-deep hover:underline">
                    查看来源 <ExternalLink size={12} />
                  </a>
                  <div className="flex gap-2">
                    <button onClick={() => void onResearch(change)}
                      className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-primary-soft px-3 text-tag font-medium text-primary-deep hover:bg-primary-tint">
                      <Search size={13} />
                      {change.research_report_id ? '查看报告' : change.research_task_id ? '继续调研' : '发起调研'}
                    </button>
                    {change.status !== 'ignored' && (
                      <button onClick={() => void onStatus(change, 'ignored')}
                        className="h-8 rounded-btn border border-line px-3 text-tag text-ink-2 hover:bg-card">忽略</button>
                    )}
                    {change.status !== 'read' && (
                      <button onClick={() => void onStatus(change, 'read')}
                        className="h-8 rounded-btn bg-primary px-3 text-tag text-white hover:bg-primary-deep">标为已读</button>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )
      })}
      {changes.length === 0 && <div className="border border-dashed border-line py-16 text-center text-aux text-ink-3">暂无变化记录</div>}
    </div>
  )
}

function DiffBlock({ title, text, tone }: { title: string; text: string; tone: string }) {
  return (
    <div className="min-w-0">
      <div className={`text-tag font-semibold ${tone}`}>{title}</div>
      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap border border-line bg-card p-3 font-mono text-tag leading-relaxed text-ink-2">
        {text || '无摘要'}
      </pre>
    </div>
  )
}

function SubscriptionEditor({ subscription, onClose, onSaved }: {
  subscription: Subscription | null
  onClose: () => void
  onSaved: (subId: string) => Promise<void>
}) {
  const editing = Boolean(subscription)
  const [query, setQuery] = useState(subscription?.query ?? '')
  const [brandsText, setBrandsText] = useState(subscription?.brands.join('、') ?? '')
  const [timezone, setTimezone] = useState(subscription?.timezone ?? 'Asia/Shanghai')
  const [scheduleType, setScheduleType] = useState<'interval' | 'daily'>(subscription?.schedule_type ?? 'interval')
  const [intervalMinutes, setIntervalMinutes] = useState(subscription?.schedule_config.interval_minutes ?? 1440)
  const [dailyTime, setDailyTime] = useState(subscription?.schedule_config.time ?? '09:00')
  const [targets, setTargets] = useState<Array<{ brand: string; target_type: MonitorTargetType; url: string }>>([])
  const [enabled, setEnabled] = useState(true)
  const [candidates, setCandidates] = useState<TargetCandidate[]>([])
  const [discovering, setDiscovering] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const brands = useMemo(
    () => brandsText.split(/[，,、\s]+/).map((item) => item.trim()).filter(Boolean),
    [brandsText],
  )

  function addTarget(target?: Partial<{ brand: string; target_type: MonitorTargetType; url: string }>) {
    setTargets((items) => [...items, {
      brand: target?.brand ?? brands[0] ?? '',
      target_type: target?.target_type ?? 'official',
      url: target?.url ?? '',
    }])
  }

  async function discover() {
    if (!query.trim() && brands.length === 0) return
    setDiscovering(true)
    setError('')
    try {
      setCandidates(await discoverSubscriptionTargets(query.trim(), brands))
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setDiscovering(false)
    }
  }

  async function submit() {
    if (!query.trim()) {
      setError('请输入订阅主题')
      return
    }
    setSaving(true)
    setError('')
    try {
      if (subscription) {
        const updated = await updateSubscription(subscription.sub_id, {
          query: query.trim(),
          brands,
          timezone,
          schedule: scheduleType === 'interval'
            ? { type: 'interval', interval_minutes: intervalMinutes }
            : { type: 'daily', time: dailyTime },
        })
        await onSaved(updated.sub_id)
      } else {
        const created = await createSubscription({
          query: query.trim(),
          brands,
          timezone,
          schedule: scheduleType === 'interval'
            ? { type: 'interval', interval_minutes: intervalMinutes }
            : { type: 'daily', time: dailyTime },
          targets: targets.filter((target) => target.brand.trim() && target.url.trim()),
          enabled,
        })
        await onSaved(created.sub_id)
      }
    } catch (requestError) {
      setError(errorMessage(requestError))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-ink/35 p-4" role="dialog" aria-modal="true">
      <div className="max-h-[92vh] w-full max-w-3xl overflow-y-auto rounded-card border border-line bg-card shadow-float">
        <div className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-card px-5 py-4">
          <h2 className="text-h3 text-ink">{editing ? '编辑订阅' : '新建订阅'}</h2>
          <button title="关闭" onClick={onClose} className="grid h-8 w-8 place-items-center text-ink-3 hover:text-ink"><X size={18} /></button>
        </div>
        <div className="space-y-5 p-5">
          {error && <div className="border border-risk/30 bg-risk/10 px-3 py-2 text-aux text-risk">{error}</div>}
          <Field label="订阅主题">
            <input value={query} onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：AI 编程助手竞争动态"
              className="h-10 w-full border border-line bg-bg px-3 text-aux text-ink outline-none focus:border-primary" />
          </Field>
          <Field label="品牌">
            <input value={brandsText} onChange={(event) => setBrandsText(event.target.value)}
              placeholder="Trae、Cursor、GitHub Copilot"
              className="h-10 w-full border border-line bg-bg px-3 text-aux text-ink outline-none focus:border-primary" />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="执行计划">
              <div className="flex h-10 border border-line bg-bg p-1">
                <button onClick={() => setScheduleType('interval')}
                  className={`flex-1 text-tag font-medium ${scheduleType === 'interval' ? 'bg-card text-primary-deep shadow-sm' : 'text-ink-3'}`}>固定间隔</button>
                <button onClick={() => setScheduleType('daily')}
                  className={`flex-1 text-tag font-medium ${scheduleType === 'daily' ? 'bg-card text-primary-deep shadow-sm' : 'text-ink-3'}`}>每天</button>
              </div>
            </Field>
            <Field label={scheduleType === 'interval' ? '间隔分钟' : '执行时间'}>
              {scheduleType === 'interval' ? (
                <input type="number" min={1} max={525600} value={intervalMinutes}
                  onChange={(event) => setIntervalMinutes(Number(event.target.value))}
                  className="h-10 w-full border border-line bg-bg px-3 text-aux outline-none focus:border-primary" />
              ) : (
                <input type="time" value={dailyTime} onChange={(event) => setDailyTime(event.target.value)}
                  className="h-10 w-full border border-line bg-bg px-3 text-aux outline-none focus:border-primary" />
              )}
            </Field>
          </div>
          <Field label="时区">
            <select value={timezone} onChange={(event) => setTimezone(event.target.value)}
              className="h-10 w-full border border-line bg-bg px-3 text-aux outline-none focus:border-primary">
              <option value="Asia/Shanghai">Asia/Shanghai</option>
              <option value="Asia/Tokyo">Asia/Tokyo</option>
              <option value="Europe/London">Europe/London</option>
              <option value="America/New_York">America/New_York</option>
              <option value="UTC">UTC</option>
            </select>
          </Field>

          {!editing && (
            <section className="border-t border-line pt-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-aux font-semibold text-ink">监控目标</h3>
                <div className="flex gap-2">
                  <button onClick={() => void discover()} disabled={discovering}
                    className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-line px-3 text-tag text-ink-2 hover:bg-primary-tint disabled:opacity-40">
                    {discovering ? <LoaderCircle size={14} className="animate-spin" /> : <Search size={14} />} 联网发现
                  </button>
                  <button onClick={() => addTarget()}
                    className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-line px-3 text-tag text-ink-2 hover:bg-primary-tint">
                    <Plus size={14} /> 添加
                  </button>
                </div>
              </div>

              {candidates.length > 0 && (
                <div className="mt-3 max-h-44 overflow-y-auto border border-line bg-bg">
                  {candidates.map((candidate) => {
                    const added = targets.some((target) => target.url === candidate.url)
                    return (
                      <div key={candidate.url} className="flex items-start gap-3 border-b border-line px-3 py-2.5 last:border-b-0">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-aux font-medium text-ink">{candidate.title || candidate.url}</div>
                          <div className="mt-0.5 truncate text-tag text-ink-3">{candidate.brand} · {TARGET_LABEL[candidate.target_type]} · {candidate.url}</div>
                        </div>
                        <button title={added ? '已添加' : '添加候选目标'} disabled={added}
                          onClick={() => addTarget(candidate)}
                          className="grid h-7 w-7 shrink-0 place-items-center text-primary-deep disabled:text-ink-3">
                          {added ? <Check size={15} /> : <Plus size={15} />}
                        </button>
                      </div>
                    )
                  })}
                </div>
              )}

              <div className="mt-3 space-y-2">
                {targets.map((target, index) => (
                  <div key={index} className="grid gap-2 border border-line bg-bg p-2 sm:grid-cols-[120px_110px_minmax(0,1fr)_32px]">
                    <input value={target.brand} onChange={(event) => setTargets((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, brand: event.target.value } : item))}
                      placeholder="品牌" className="h-8 border border-line bg-card px-2 text-tag outline-none focus:border-primary" />
                    <select value={target.target_type} onChange={(event) => setTargets((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, target_type: event.target.value as MonitorTargetType } : item))}
                      className="h-8 border border-line bg-card px-2 text-tag outline-none focus:border-primary">
                      {Object.entries(TARGET_LABEL).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                    </select>
                    <input value={target.url} onChange={(event) => setTargets((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, url: event.target.value } : item))}
                      placeholder="https://..." className="h-8 border border-line bg-card px-2 text-tag outline-none focus:border-primary" />
                    <button title="移除" onClick={() => setTargets((items) => items.filter((_, itemIndex) => itemIndex !== index))}
                      className="grid h-8 w-8 place-items-center text-ink-3 hover:text-risk"><Trash2 size={14} /></button>
                  </div>
                ))}
                {targets.length === 0 && <div className="border border-dashed border-line py-7 text-center text-tag text-ink-3">尚未添加监控目标</div>}
              </div>
              <label className="mt-4 flex items-center gap-2 text-aux text-ink-2">
                <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)}
                  className="h-4 w-4 accent-primary" />
                创建后启用
              </label>
            </section>
          )}
        </div>
        <div className="sticky bottom-0 flex justify-end gap-2 border-t border-line bg-card px-5 py-4">
          <button onClick={onClose} className="h-9 rounded-btn border border-line px-4 text-aux text-ink-2 hover:bg-bg">取消</button>
          <button onClick={() => void submit()} disabled={saving}
            className="inline-flex h-9 items-center gap-2 rounded-btn bg-primary px-4 text-aux font-medium text-white hover:bg-primary-deep disabled:opacity-50">
            {saving && <LoaderCircle size={15} className="animate-spin" />} 保存
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-tag font-medium text-ink-2">{label}</span>
      {children}
    </label>
  )
}

