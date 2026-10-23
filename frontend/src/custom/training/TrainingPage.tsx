import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { EChartsCandlestick, type ChartMarker } from '@/components/EChartsCandlestick'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { finishTrainingQuestion, initialTrainingSession, mergeTrainingAccount, trainingSessionConflict } from '@/lib/training-account'
import { mergeTrainingStep, shareTrainingView } from '@/lib/training-step'
import { trainingPriceLines } from '@/lib/training-chart'
import { TrainingAccountPanel } from './TrainingAccountPanel'
import type { TrainingAction, TrainingCreate, TrainingDraft, TrainingMode, TrainingPractice, TrainingView, TrainingListItem, TrainingAccountData } from '@/lib/training-types'

const TrainingChart = memo(EChartsCandlestick)

const MODE: Record<TrainingMode, string> = { close_t: '看完整日线 · 当日收盘模拟', 'open_t+1': '收盘决策 · 次日开盘成交' }
const LABEL: Partial<Record<TrainingAction, string>> = { buy: '买入', sell: '卖出', hold: '继续持有', reject: '放弃', note: '观察', cancel: '取消委托' }
const TAGS = ['平台突破', '均线修复', '放量确认', '缩量回踩', '超跌反弹', '趋势延续', '破位止损', '止盈', '判断失效', '情绪冲动']
const CHOICES = [['vol', '成交量'], ['macd', 'MACD'], ['kdj', 'KDJ'], ['rsi', 'RSI'], ['boll', 'BOLL（主图）']]
const BOARDS = [['sh_main', '沪市主板'], ['sz_main', '深市主板'], ['growth', '创业板'], ['star', '科创板'], ['beijing', '北交所']]
const PRACTICE: Record<TrainingPractice, string> = { single: '单票连续练习', screening: '多票比较选股' }
const btn = 'rounded-btn border border-border bg-elevated px-3 py-2 text-sm hover:bg-border disabled:opacity-40 disabled:cursor-not-allowed'
const field = 'rounded-btn border border-border bg-surface px-3 py-2 text-sm text-foreground w-full'
const box = 'rounded-card border border-border bg-surface p-4'
const pct = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(2)}%`
const cash = (v: number) => v.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const uid = () => crypto.randomUUID()
const EMPTY_DRAFT: TrainingDraft = { reason: '', invalidation: '', tags: [], confidence: 3, size: .5 }

export default function TrainingPage() {
  const [params, setParams] = useSearchParams()
  const verification = params.get('verify') === '1'
  const requestedSession = params.get('session') ?? ''
  const restoredAccount = useRef(false)
  const [extrasOpen, setExtrasOpen] = useState(false)
  const qc = useQueryClient()
  const navigate = useNavigate()
  const sessionStorage = verification ? storage.trainingVerificationSession : storage.trainingSession
  const preparationStorage = verification ? storage.trainingVerificationPreparation : storage.trainingPreparation
  const [preparationId, setPreparationId] = useState(() => preparationStorage.get(''))
  const [preloadId, setPreloadId] = useState('')
  const preloadAttempt = useRef('')
  const preloadSettingsRef = useRef('')
  const [sessionId, setSessionId] = useState(() => requestedSession || sessionStorage.get(''))
  const [saved] = useState(() => storage.trainingSettings.get({}))
  const [ids, setIds] = useState<string[]>(saved.strategy_ids ?? [])
  const [mode, setMode] = useState<TrainingMode>(saved.mode ?? 'close_t')
  const [horizon, setHorizon] = useState(saved.horizon ?? 60)
  const [autoScan, setAutoScan] = useState(saved.auto_scan ?? true)
  const [practice, setPractice] = useState<TrainingPractice>(saved.practice_kind ?? 'single')
  const [selectionType, setSelectionType] = useState(saved.selection_type ?? 'random')
  const [strategySource, setStrategySource] = useState<'cached' | 'live'>(saved.strategy_source ?? 'cached')
  const [candidateCount, setCandidateCount] = useState(saved.candidate_count ?? 10)
  const [boards, setBoards] = useState(saved.boards ?? BOARDS.map(b => b[0]))
  const [excludeSt, setExcludeSt] = useState(saved.exclude_st ?? true)
  const [historyStart, setHistoryStart] = useState(saved.history_start ?? '')
  const [historyEnd, setHistoryEnd] = useState(saved.history_end ?? '')
  const [scanMin, setScanMin] = useState(saved.scan_interval_min ?? 3)
  const [scanMax, setScanMax] = useState(saved.scan_interval_max ?? 10)
  const [alias, setAlias] = useState('')
  const [chartRange, setChartRange] = useState<number | 'all'>(120)
  const historyAttempt = useRef('')
  const draftStorage = verification ? storage.trainingVerificationDrafts : storage.trainingDrafts
  const [drafts, setDrafts] = useState(() => draftStorage.get({}))
  const [onlySelectedRecords, setOnlySelectedRecords] = useState(true)
  const [error, setError] = useState('')
  const [aiText, setAiText] = useState('')
  const strategies = useQuery({ queryKey: QK.trainingStrategies, queryFn: api.trainingStrategies, staleTime: 30000 })
  const selectionTypes = useQuery({ queryKey: QK.trainingSelectionTypes, queryFn: api.trainingSelectionTypes, staleTime: Infinity })
  const sessions = useQuery({ queryKey: QK.trainingSessions, queryFn: api.trainingSessions, enabled: extrasOpen })
  const summary = useQuery({ queryKey: QK.trainingSummary, queryFn: api.trainingSummary, enabled: extrasOpen })
  const session = useQuery({ queryKey: QK.trainingSession(sessionId), queryFn: () => api.trainingSession(sessionId),
    enabled: !!sessionId, retry: false, structuralSharing: shareTrainingView })
  const v = session.data
  const accountQuery = useQuery({ queryKey: QK.trainingAccount(verification),
    queryFn: () => api.trainingAccount(verification), retry: 1 })
  useEffect(() => {
    if (!accountQuery.isSuccess || restoredAccount.current || preparationId) return
    restoredAccount.current = true
    setSessionId(id => initialTrainingSession(id, requestedSession, accountQuery.data.account))
  }, [accountQuery.isSuccess, accountQuery.data?.account, preparationId, requestedSession])
  useEffect(() => { if (requestedSession) setSessionId(requestedSession) }, [requestedSession])
  function openSession(id: string) {
    setSessionId(id)
    setParams(previous => { const next = new URLSearchParams(previous); next.set('session', id); return next }, { replace: true })
  }
  const conflict = trainingSessionConflict(v, accountQuery.data?.account)
  async function finishCurrentQuestion(allowNew = false) {
    // Read the current pointer at the explicit switch, even if another tab advanced it.
    const data = await api.trainingAccount(verification)
    qc.setQueryData(QK.trainingAccount(verification), data)
    if (trainingSessionConflict(v, data.account) && data.account?.status === 'active') {
      openSession(data.account.current_session_id)
    }
    if (allowNew && data.account?.status !== 'active' && v?.finished) return undefined
    return finishTrainingQuestion(v, data.account, async current => {
      const finished = await api.trainingCommand(current.id,
        { action: 'finish', continuous_account: true, revision: current.revision, request_id: uid() })
      update(finished)
      return finished
    })
  }
  const stopAccount = useMutation({ mutationFn: () => {
    const account = accountQuery.data?.account
    if (!account) throw new Error('尚无持续账户')
    return api.trainingAccountStop(account.id, { revision: account.revision, request_id: uid() })
  }, onSuccess: result => {
    qc.setQueryData(QK.trainingAccount(verification), result)
    void qc.invalidateQueries({ queryKey: QK.trainingAccount(verification) })
    void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) })
    void qc.invalidateQueries({ queryKey: QK.trainingSessions })
    void qc.invalidateQueries({ queryKey: QK.trainingSummary })
    if (result.account) void qc.invalidateQueries({ queryKey: QK.trainingAccountRecords(result.account.id) })
    setError('')
  }, onError: e => { setError(e.message); void accountQuery.refetch() } })
  const recordGroups = useMemo(() => {
    const groups = new Map<string, { id: string; name: string; verification: boolean; items: TrainingListItem[] }>()
    for (const item of sessions.data ?? []) {
      if (!verification && item.verification) continue
      const savedGroup = item.record_group ?? {
        id: JSON.stringify([item.selection_type, [...item.strategies].sort()]),
        name: item.strategies.join('、') || item.selection_name,
      }
      const id = `${item.verification ? 'verification' : 'personal'}:${savedGroup.id}`
      const group = groups.get(id)
      if (group) group.items.push(item)
      else groups.set(id, { id, name: savedGroup.name, verification: item.verification, items: [item] })
    }
    return [...groups.values()]
  }, [sessions.data, verification])
  const draftKey = `${sessionId}:${alias}`
  const lastRecord = [...(v?.events ?? [])].reverse().find(e => e.alias === alias)
  const draft: TrainingDraft = drafts[draftKey] ?? (lastRecord ? {
    reason: lastRecord.reason, invalidation: lastRecord.invalidation ?? '', tags: lastRecord.tags,
    confidence: lastRecord.confidence ?? 3, size: lastRecord.size ?? .5, pattern: lastRecord.pattern ?? '',
  } : EMPTY_DRAFT)
  const { reason, invalidation, tags, confidence, size, pattern = '' } = draft
  function editDraft(patch: Partial<TrainingDraft>) {
    if (!sessionId || !alias) return
    setDrafts(previous => ({ ...previous, [draftKey]: { ...draft, ...patch } }))
  }
  useEffect(() => { draftStorage.set(drafts) }, [draftStorage, drafts])
  const preparation = useQuery({ queryKey: QK.trainingPreparation(preparationId),
    queryFn: () => api.trainingPreparation(preparationId), enabled: !!preparationId, retry: 1,
    refetchInterval: query => ['queued', 'running', 'cancelling'].includes(query.state.data?.status ?? 'queued') ? 1000 : false })

  useEffect(() => {
    sessionStorage.set(sessionId)
    setAlias(''); setAiText(''); setError('')
  }, [sessionId])
  useEffect(() => {
    if (v && !v.candidates.some(c => c.alias === alias)) setAlias(v.position?.alias ?? v.candidates[0]?.alias ?? '')
  }, [v, alias])

  function update(view: TrainingView, refreshAccountRecords = true) {
    qc.setQueryData(QK.trainingSession(view.id), view)
    // The command already returns current progress; avoid decoding every session.
    qc.setQueryData<TrainingListItem[]>(QK.trainingSessions, previous => {
      if (!previous) return previous
      const item: TrainingListItem = { id: view.id, record_group: view.record_group, mode: view.mode, cursor: view.cursor, finished: view.finished,
        strategies: view.strategies.map(s => s.name), total_return: view.total_return, verification: view.verification,
        selection_type: view.selection.type, selection_name: view.selection.name ?? '',
        practice_kind: view.practice_kind, question_number: view.question_number, replay_of: view.replay_of, replay_number: view.replay_number }
      return previous.some(s => s.id === view.id) ? previous.map(s => s.id === view.id ? item : s) : [item, ...previous]
    })
    if (view.account) {
      for (const id of ['', view.account.id]) {
        let reload = false
        qc.setQueryData<TrainingAccountData>(QK.trainingAccount(verification, id), previous => {
          const merged = mergeTrainingAccount(previous, view.account!)
          if (!merged) reload = true
          return merged ?? previous
        })
        if (reload) void qc.invalidateQueries({ queryKey: QK.trainingAccount(verification, id) })
      }
      if (refreshAccountRecords) void qc.invalidateQueries({ queryKey: QK.trainingAccountRecords(view.account.id) })
    }
    if (view.finished) void qc.invalidateQueries({ queryKey: QK.trainingSummary })
    setError('')
  }
  useEffect(() => { preparationStorage.set(preparationId) }, [preparationId])
  useEffect(() => {
    if (!preparationId) return
    const result = preparation.data
    if (result?.status === 'complete' && result.result) {
      update(result.result); openSession(result.result.id); setPreparationId('')
    } else if (result?.status === 'failed' || result?.status === 'cancelled') {
      setError(result.error || result.phase); setPreparationId('')
    } else if (preparation.error?.message.includes('已失效')) {
      setError(preparation.error.message); setPreparationId('')
    }
  }, [preparationId, preparation.data, preparation.error])
  function makeConfig(previous?: TrainingView, requestId: string = uid()): TrainingCreate {
    const historyBars = chartRange === 'all' || chartRange === 500 ? 500 : chartRange === 250 ? 250 : 120
    const knownAccount = previous?.account ?? v?.account ?? accountQuery.data?.account
    const currentAccountId = knownAccount?.status === 'active' ? knownAccount.id : undefined
    return previous ? { continuous_account: true, account_id: currentAccountId,
        strategy_ids: previous.strategies.map(s => s.id), mode: previous.mode, horizon: previous.horizon, history_bars: historyBars,
        practice_kind: previous.practice_kind, selection_type: previous.selection.type, boards: previous.selection.boards,
        strategy_source: previous.strategy_source,
        exclude_st: previous.selection.exclude_st, history_start: previous.selection.history_start,
        history_end: previous.selection.history_end, candidate_count: previous.candidate_count,
        auto_scan: previous.auto_scan, scan_interval_min: previous.scan_interval_min, scan_interval_max: previous.scan_interval_max,
        request_id: requestId, verification, indicators: previous.indicators, previous_session_id: previous.id,
      } : { continuous_account: true, account_id: currentAccountId, history_bars: historyBars, strategy_ids: selectionType === 'strategy' ? ids : [], mode, horizon,
        practice_kind: practice, selection_type: selectionType, boards, exclude_st: excludeSt,
        strategy_source: strategySource,
        history_start: historyStart || null, history_end: historyEnd || null,
        candidate_count: practice === 'single' ? 1 : candidateCount, auto_scan: practice === 'screening' && autoScan,
        scan_interval_min: scanMin, scan_interval_max: scanMax,
        request_id: requestId, verification, indicators: storage.trainingIndicators.get(['vol', 'macd']) }
  }
  const preload = useMutation({
    mutationFn: (request: { config: TrainingCreate; context: string; settings: string }) => api.trainingPreload(request.config),
    retry: 3, retryDelay: 3000,
    onSuccess: (job, request) => {
      if (preloadAttempt.current !== request.context) {
        if (preloadSettingsRef.current !== request.settings) void api.trainingCancelPreparation(job.id).catch(() => {})
        return
      }
      qc.setQueryData(QK.trainingPreparation(job.id), job); setPreloadId(job.id)
    },
  })
  const preloadJob = useQuery({ queryKey: QK.trainingPreparation(preloadId),
    queryFn: () => api.trainingPreparation(preloadId), enabled: !!preloadId, retry: 1,
    refetchInterval: query => ['queued', 'running', 'cancelling'].includes(query.state.data?.status ?? 'queued') ? 1000 : false })
  const create = useMutation({
    mutationFn: async (previous?: TrainingView) => {
      const finished = await finishCurrentQuestion(true)
      const config = { ...makeConfig(previous), ...(finished?.account ? { account_id: finished.account.id } : {}) }
      return api.trainingPrepare({ ...config, preload_id: preloadId || null }).then(job => {
        if (!verification) storage.trainingSettings.set({ ...config, previous_session_id: undefined, account_id: undefined, request_id: undefined })
        return job
      })
    },
    onSuccess: job => { setError(''); qc.setQueryData(QK.trainingPreparation(job.id), job); setPreparationId(job.id) }, onError: e => setError(e.message),
  })
  const nextQuestion = useMutation({
    mutationFn: async () => {
      if (!v) throw new Error('请先开始训练')
      const previous = await finishCurrentQuestion()
      if (!previous) throw new Error('请先开始训练')
      return api.trainingPrepare({ ...makeConfig(previous), preload_id: preloadId || null })
    },
    onSuccess: job => { setError(''); qc.setQueryData(QK.trainingPreparation(job.id), job); setPreparationId(job.id) },
    onError: e => { setError(e.message); void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) }); void accountQuery.refetch() },
  })
  const replay = useMutation({ mutationFn: (stockAlias: string) => {
    if (conflict) throw new Error('请返回账户当前题后重训')
    if (!v?.finished) throw new Error('请先结束并揭示')
    return api.trainingReplay(v.id, { alias: stockAlias, continuous_account: true, account_id: v.account?.status === 'active' ? v.account.id : undefined, revision: v.revision, request_id: uid() })
  }, onSuccess: view => { update(view); openSession(view.id) }, onError: e => {
    setError(e.message); void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) }); void accountQuery.refetch()
  } })
  const cancelPreparation = useMutation({ mutationFn: () => api.trainingCancelPreparation(preparationId),
    onSuccess: job => qc.setQueryData(QK.trainingPreparation(job.id), job), onError: e => setError(e.message) })
  const command = useMutation({
    mutationFn: (payload: { action: TrainingAction; indicators?: string[] }) => {
      if (!v) throw new Error('请先开始训练')
      return api.trainingCommand(v.id, { ...payload, revision: v.revision, request_id: uid(), alias, reason,
        tags, invalidation, confidence, size, pattern, continuous_account: true, price_unit: v.price_unit, step_delta: payload.action === 'advance' }).then(view => mergeTrainingStep(v, view))
    },
    onSuccess: (view, payload) => {
      update(view, payload.action !== 'advance' || !!v?.pending)
      if (payload.action === 'indicators') storage.trainingIndicators.set(view.indicators)
    },
    onError: e => { setError(e.message); void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) }); void accountQuery.refetch() },
  })
  const analyze = useMutation({ mutationFn: () => api.trainingAnalyze(sessionId), onSuccess: r => {
    setAiText(r.content); void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) })
  }, onError: e => setError(e.message) })
  const history = useMutation({ mutationFn: (range: number | 'all') => {
    if (!v || !alias) throw new Error('请选择当前可见候选')
    return api.trainingHistory(v.id, { alias, revision: v.revision, request_id: uid(), bars: range === 'all' ? null : range })
  }, onMutate: () => setError(''), onSuccess: view => update(view, false), onError: e => {
    setError(e.message); void qc.invalidateQueries({ queryKey: QK.trainingSession(sessionId) }); void accountQuery.refetch()
  } })
  const download = useMutation({ mutationFn: () => api.trainingExport(sessionId), onSuccess: data => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a'); link.href = url; link.download = `盘感训练_${sessionId.slice(0, 8)}.json`; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }, onError: e => setError(e.message) })
  const busy = stopAccount.isPending || replay.isPending || create.isPending || nextQuestion.isPending || !!preparationId || command.isPending || history.isPending || session.isFetching || accountQuery.isLoading
  const selected = v?.candidates.find(c => c.alias === alias)
  const selectedOutcome = v?.review?.future_outcomes?.find(result => result.alias === alias)
  const needsHistory = !!selected && (selected.history_window ?? (selected.history_loaded ? 'all' : 120)) !== chartRange
  useEffect(() => {
    if (!v || !needsHistory || busy) return
    const key = `${v.id}:${alias}:${chartRange}`
    // One attempt per user-selected context; failures stay visible for manual retry.
    if (historyAttempt.current === key) return
    historyAttempt.current = key
    history.mutate(chartRange)
  }, [v?.id, alias, chartRange, needsHistory, busy])
  const active = !!v && !v.finished && !conflict
  const canCreate = boards.length > 0 && (selectionType !== 'strategy' || ids.length > 0) && scanMin <= scanMax && (!historyStart || !historyEnd || historyStart <= historyEnd)
  const preloadSettings = JSON.stringify({ ...makeConfig(v, ''), request_id: undefined, previous_session_id: undefined, indicators: undefined })
  const preloadContext = (v?.id ?? '') + ':' + preloadSettings
  useEffect(() => {
    if (!v || conflict || !restoredAccount.current || busy || preloadAttempt.current === preloadContext) return
    preloadAttempt.current = preloadContext
    preloadSettingsRef.current = preloadSettings
    setPreloadId('')
    preload.mutate({ config: makeConfig(v), context: preloadContext, settings: preloadSettings })
  }, [v?.id, preloadContext, busy, conflict])
  const preloadLabel = preloadJob.data?.preload_ready ? '下一题及所选K线已预加载' :
    preloadJob.data?.status === 'queued' ? '预加载排队中…' :
    preload.isPending || preloadJob.data?.status === 'running' ? '后台预加载下一题…' :
    preload.isError || preloadJob.isError || preloadJob.data?.status === 'failed' ? '下一题将在点击时准备' : '等待预加载下一题'
  const chartPriceLines = useMemo(() => trainingPriceLines(selected, v?.position, !!v?.account_settlement),
    [selected?.bars, selected?.alias, v?.position, v?.account_settlement])
  const markers = useMemo<ChartMarker[]>(() => (v?.events ?? []).flatMap(e => {
    const fillDay = /^T\+(\d+) 已成交$/.exec(e.outcome)
    return e.alias === alias && fillDay && (e.action === 'buy' || e.action === 'sell')
      ? [{ date: `T+${fillDay[1]}`, kind: e.action, label: `${LABEL[e.action]} ${e.id}` }] : []
  }), [v?.events, alias])
  const chartInfo = useMemo(() => ({ name: v?.finished ? v.review?.identities.find(i => i.alias === alias)?.name : `候选 ${alias}` }),
    [v?.finished, v?.review?.identities, alias])
  function decide(action: TrainingAction) {
    if (!reason.trim()) { setError('请先写下本次判断理由'); return }
    command.mutate({ action })
  }

  return <div className="space-y-4 p-4 md:p-6 min-w-0">
    <div><h1 className="text-xl font-semibold">盘感训练</h1><p className="text-sm text-muted mt-1">单一持续账户 · 跨题累计资金 · 隐藏股票与日期 · 按模式保存操作理由</p></div>
    <div className="flex flex-wrap gap-4 text-sm text-blue-400"><a href="#training-account">查看收益曲线 ↓</a><Link to={`/training/account${verification ? '?verify=1' : ''}`}>训练账户详情 ↗</Link></div>
    {verification && <p className="text-sm text-amber-400">功能验证场：独立保存进度，不计入你的训练胜率与收益汇总。</p>}
    {!!preparationId && <section className={box} role="status"><strong>{preparation.data?.phase || '读取抽题进度…'}</strong>
      <p className="text-sm text-muted mt-1">已用 {preparation.data?.elapsed ?? 0} 秒{preparation.data?.total ? ` · 已载入 ${preparation.data.completed}/${preparation.data.total}` : ''}。可以离开页面，回来后恢复进度。</p>
      {preparation.error && <p className="text-sm text-red-400">{preparation.error.message}<button className={`${btn} ml-2`} onClick={() => void preparation.refetch()}>重试查询</button></p>}
      <button className={`${btn} mt-2`} disabled={cancelPreparation.isPending || preparation.data?.status === 'cancelling'} onClick={() => cancelPreparation.mutate()}>取消本次抽题</button>
    </section>}
    <details className={box} open={!active}>
      <summary className="cursor-pointer font-medium">开始新训练</summary>
      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <label className="text-sm">训练方式<select aria-label="训练方式" value={practice} onChange={e => setPractice(e.target.value as TrainingPractice)} className={field}>{Object.entries(PRACTICE).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="text-sm">选股类型<select aria-label="选股类型" value={selectionType} disabled={selectionTypes.isPending} onChange={e => setSelectionType(e.target.value)} className={field}>
          {[...new Set(selectionTypes.data?.map(t => t.group))].map(group => <optgroup key={group} label={group}>{selectionTypes.data?.filter(t => t.group === group).map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</optgroup>)}
        </select></label>
        <label className="text-sm">每批候选数量<select aria-label="每批候选数量" value={practice === 'single' ? 1 : candidateCount} disabled={practice === 'single'} onChange={e => setCandidateCount(Number(e.target.value))} className={field}>{[1, 3, 5, 10, 20].map(n => <option key={n} value={n}>{n} 只</option>)}</select></label>
        <label className="text-sm">成交口径<select aria-label="成交口径" value={mode} onChange={e => setMode(e.target.value as TrainingMode)} className={field}>{Object.entries(MODE).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label className="text-sm">训练长度<select aria-label="训练长度" value={horizon} onChange={e => setHorizon(Number(e.target.value))} className={field}>{[30, 60, 90, 120].map(n => <option key={n} value={n}>{n} 个交易日</option>)}</select></label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={practice === 'single'} checked={practice === 'screening' && autoScan} onChange={e => setAutoScan(e.target.checked)} />按随机间隔重新选股</label>
      </div>
      <p className="text-xs text-muted mt-2">{practice === 'single' ? '每题只练一只股票，逐日推演，结束复盘后沿用设置进入下一题。' : '同一历史时点比较多只候选，自主记录选或不选的理由；可不定期重选。数量是上限，条件不足时不补造候选。'}</p>
      <p className="text-xs text-muted mt-2">{selectionType === 'strategy' && strategySource === 'cached' ? '从所选策略已计算的历史命中列表中抽题。' : selectionTypes.data?.find(t => t.id === selectionType)?.description} 选股条件只用于抽取训练题目，不代表买入信号。</p>
      {selectionTypes.error && <p className="text-red-400 text-sm">{selectionTypes.error.message}</p>}
      <div className="flex flex-wrap gap-3 my-3 text-sm">{BOARDS.map(([key, label]) => <label className="flex items-center gap-1" key={key}><input type="checkbox" checked={boards.includes(key)} onChange={() => setBoards(boards.includes(key) ? boards.filter(b => b !== key) : [...boards, key])} />{label}</label>)}
        <label className="flex items-center gap-1"><input type="checkbox" checked={excludeSt} onChange={e => setExcludeSt(e.target.checked)} />剔除当前名称含 ST 的股票</label></div>
      <div className="grid gap-3 md:grid-cols-4">
        <label className="text-sm">随机起点最早日期<input type="date" aria-label="历史起始日期" className={field} value={historyStart} onChange={e => setHistoryStart(e.target.value)} /></label>
        <label className="text-sm">随机起点最晚日期<input type="date" aria-label="历史结束日期" className={field} value={historyEnd} onChange={e => setHistoryEnd(e.target.value)} /></label>
        <label className="text-sm">重选最小间隔<input type="number" min={1} max={30} aria-label="重选最小间隔" className={field} disabled={practice === 'single' || !autoScan} value={scanMin} onChange={e => setScanMin(Number(e.target.value))} /></label>
        <label className="text-sm">重选最大间隔<input type="number" min={1} max={30} aria-label="重选最大间隔" className={field} disabled={practice === 'single' || !autoScan} value={scanMax} onChange={e => setScanMax(Number(e.target.value))} /></label>
      </div>
      <p className="text-xs text-muted mt-2">日期限制训练的随机起点，不显示抽中的真实日期；{selectionType === 'strategy' && strategySource === 'cached' ? '留空从缓存覆盖日抽题。' : '留空沿用最近约 750 个市场交易日。'}历史新高/低仅指本地已有数据。ST 筛除按当前名称，历史更名尚未完整还原。</p>
      {selectionType === 'strategy' && <>
      <label className="text-sm block mt-3">策略数据来源<select aria-label="策略数据来源" className={`${field} mt-1`} value={strategySource} onChange={e => setStrategySource(e.target.value as 'cached' | 'live')}>
        <option value="cached">导入信号日抽题（推荐）</option><option value="live">随机日期逐日查询信号</option></select></label>
      <p className="text-xs text-muted mt-2">{strategySource === 'cached' ? 'T+0从所选策略的非空信号日抽取。信号与行情在本地导入时已冻结；外部信号不代表已验证策略参数或无未来函数。' : '先抽取市场日期，再按当天查询已导入信号，可能抽到空题。独立版不会加载或执行TSP的策略Python代码。'}</p>
      <p className="text-xs text-muted my-2">选择当前策略（最多 5 个，取候选并集再随机抽样）。无法保证历史时点的因子暂不开放；空选股结果可直接推进。</p>
      {strategies.isPending && <p>正在读取策略…</p>}
      {strategies.error && <p className="text-red-400">{strategies.error.message}</p>}
      <div className="max-h-48 overflow-y-auto grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {[...(strategies.data ?? [])].sort((a, b) => Number(b.available) - Number(a.available)).map(s => <label key={s.id} title={s.reason} className={`flex items-start gap-2 text-sm ${s.available ? '' : 'text-muted'}`}>
          <input type="checkbox" className="mt-1" disabled={!s.available || busy || (!ids.includes(s.id) && ids.length >= 5)} checked={ids.includes(s.id)}
            onChange={() => setIds(ids.includes(s.id) ? ids.filter(i => i !== s.id) : [...ids, s.id])} />
          <span>{s.name}{!s.available && <small className="block">{s.reason}</small>}</span>
        </label>)}
      </div>
      </>}
      {!canCreate && <p className="text-sm text-amber-400 mt-2">请选择股票板块及必要的策略，并检查日期和重选间隔。</p>}
      <button className={`${btn} mt-3`} disabled={busy || !canCreate} onClick={() => create.mutate(undefined)}>{create.isPending || preparationId ? '正在准备训练题目…' : active ? '结算本题并按新设置换题' : '随机开始一题'}</button>
      <p className="text-xs text-muted mt-2">首次账户资金 10 万；以后跨题沿用资金，只有“结束记录”才关闭账户。最多持有一只股票，100 股整手。佣金双边 0.02%、卖出税费 0.05%、滑点双边 5bp，固定为训练成本。历史 ST 名称变化未完整还原；除权期间可继续持股，净值按复权权益衔接；分红到账与配股认购未单独还原。</p>
    </details>

    {(error || session.error) && <div role="alert" className="rounded-card border border-red-400/40 p-3 text-sm text-red-400">{error || session.error?.message}</div>}
    {session.isPending && sessionId && <p className={box}>正在恢复训练…</p>}
    {conflict && v && <div className={`${box} flex flex-wrap items-center gap-3 text-sm`} role="status">
      <span>当前查看的是历史题目，原有记录已保留。</span>
      {accountQuery.data?.account?.status === 'active' && <button className={btn} disabled={busy} onClick={() => openSession(accountQuery.data!.account!.current_session_id)}>返回账户当前题</button>}
    </div>}

    {v && <>
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className={`${box} min-w-0`}>
          <div className="flex flex-wrap gap-2 mb-3">{v.candidates.map(c => <button key={c.alias} disabled={busy} className={`${btn} ${c.alias === alias ? 'border-blue-400 text-blue-400' : ''}`} onClick={() => setAlias(c.alias)}>
            {c.alias}{c.held ? ' · 持仓' : ''}{c.rejected ? ' · 已放弃' : ''}{!c.tradable ? ' · 无当日行情' : ''}
          </button>)}</div>
          <div className="flex flex-wrap gap-3 text-xs mb-3">{CHOICES.map(([key, label]) => <label key={key} className="flex items-center gap-1"><input type="checkbox" checked={v.indicators.includes(key)} disabled={busy || !active}
            onChange={() => command.mutate({ action: 'indicators', indicators: v.indicators.includes(key) ? v.indicators.filter(i => i !== key) : [...v.indicators, key] })} />{label}</label>)}</div>
          {selected ? <><div className="flex flex-wrap items-center gap-3 mb-3">
            <button className={btn} disabled={busy || selected.history_window === 'all'} onClick={() => { setChartRange('all'); history.mutate('all') }}>{history.isPending ? `正在加载${history.variables === 'all' ? '全部前史' : `${history.variables}根历史`}…` : selected.history_window === 'all' ? '全部前史已加载' : '加载全部前史'}</button>
            <label className="text-xs flex items-center gap-2">显示范围<select aria-label="K线显示范围" className={field} disabled={busy} value={chartRange} onChange={e => setChartRange(e.target.value === 'all' ? 'all' : Number(e.target.value))}>{[120, 250, 500].map(n => <option key={n} value={n}>最近 {n} 根</option>)}<option value="all">全部历史</option></select></label>
            <span className="text-xs text-muted">已加载 {selected.bars.length} 根已揭示日线 · 初始窗口 {chartRange === 'all' ? selected.bars.length : Math.min(chartRange, selected.bars.length)} 根 · 滚轮缩放，拖动向前看</span>
            {chartPriceLines[1] && <span className="text-xs text-amber-400" title="前复权持仓成本，含买入佣金和滑点，不含未来卖出费用">持仓成本 {chartPriceLines[1].value.toFixed(2)} 元</span>}
            {needsHistory && <span className="text-xs text-amber-400" role="status">{history.isPending ? '正在加载所选范围的历史…' : history.isError ? '所选范围历史加载失败' : '等待加载所选范围的历史…'}{history.isError && <button className={`${btn} ml-2`} disabled={busy} onClick={() => history.mutate(chartRange)}>重试所选范围</button>}</span>}
            {selected.history_loaded && chartRange !== 'all' && selected.bars.length < chartRange && <span className="text-xs text-muted">本地可用历史不足 {chartRange} 根，已显示全部 {selected.bars.length} 根。</span>}
          </div><TrainingChart key={`${v.id}-${alias}-${selected.history_window ?? 120}-${chartRange}`} data={selected.bars} visibleBars={chartRange} height={440} activeIndicators={v.indicators} markers={markers} priceLines={chartPriceLines} priceAxisDecimals={2} stockInfo={chartInfo} />
            <p className="text-xs text-muted">前复权价格（元） · 当日不复权收盘 {selected.bars.at(-1)?.raw_close?.toFixed(2) ?? '未提供'} 元{v.review ? ` · 最后一根对应 ${v.review.end}` : ''}。使用本题冻结行情，对照软件需选择相同日期与复权口径。图表与指标只含已揭示日线。BOLL 叠加主图，MACD/KDJ/RSI 在底部联动显示。</p></>
            : <div className="h-72 flex items-center justify-center text-muted">当前设置无候选。多票模式可推进或重选，单票模式可结束空题后进入下一题。</div>}
          <div role="region" aria-label="训练操作条" className="sticky bottom-0 z-20 mt-2 rounded-btn border border-border bg-surface p-2 shadow-lg">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                <strong>第 {v.account_question ?? v.question_number} 题 · T+{v.cursor}/{v.horizon}</strong>
                <span>{v.finished ? '已结束' : '训练中'}</span>
                {!!v.replay_number && <span className="text-amber-400">重训第 {v.replay_number} 次 · 已看结果</span>}
                <span>净值 <b>{cash(v.equity)}</b></span><span>现金 <b>{cash(v.account_settlement ? v.equity : v.cash)}</b></span>
                <span>{v.account?.current_session_id === v.id ? '累计收益' : '本题收益'} <b>{pct(v.account?.current_session_id === v.id ? v.account?.total_return : v.total_return)}</b></span><span>持仓 <b>{v.account_settlement ? '已按市值结算' : v.position ? `${v.position.alias} · ${pct(v.position.pnl)}` : '空仓'}</b></span>
              </div>
              <div className="flex flex-wrap gap-1">
                <span className={`self-center text-xs mr-1 ${preloadJob.data?.preload_ready ? 'text-emerald-400' : 'text-muted'}`}>{preloadLabel}</span>
                <button className={`${btn} px-2 py-1 text-xs`} disabled={busy || !active || v.practice_kind === 'single'} onClick={() => command.mutate({ action: 'scan' })}>按设置重选</button>
                <button className={`${btn} px-3 py-1 text-xs border-blue-400 text-blue-400`} disabled={busy || !active || v.cursor >= v.horizon} onClick={() => command.mutate({ action: 'advance' })}>下一交易日 →</button>
                <button className={`${btn} px-2 py-1 text-xs`} disabled={busy || !active} onClick={() => command.mutate({ action: 'finish' })}>结束并揭示</button>
                {v.finished && selected && <button className={`${btn} px-2 py-1 text-xs`} disabled={busy || conflict} onClick={() => replay.mutate(alias)}>{replay.isPending ? '正在重训…' : '重训当前股票'}</button>}
                <button className={`${btn} px-2 py-1 text-xs border-blue-400 text-blue-400`} disabled={busy || conflict} onClick={() => nextQuestion.mutate()}>{nextQuestion.isPending ? '正在切换下一题…' : v.finished ? '下一题 →' : '结束本题并下一题 →'}</button>
              </div>
            </div>
            {selectedOutcome && <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 mt-2 text-xs" aria-label="当前股票后续表现">
              <strong>{alias} · T+0 收盘至 T+{v.horizon}</strong>
              <span className="text-red-400">最大涨幅 {pct(selectedOutcome.max_rise)}</span>
              <span className="text-emerald-400">最大跌幅 {pct(selectedOutcome.max_fall)}</span>
              <span>最终涨跌幅 {pct(selectedOutcome.final_return)}</span>
              {selectedOutcome.data_status !== 'complete' && <span className="text-amber-400">{selectedOutcome.data_status === 'missing_start' ? '基准行情不足' : selectedOutcome.data_status === 'partial_prices' ? '极值行情不完整' : `最后有价 T+${selectedOutcome.last_available_day}`}</span>}
              <a href="#training-outcomes" className="text-blue-400 underline">详细表现</a>
            </div>}
            {(v.position || v.pending) && !v.finished && <p className="text-xs text-muted mt-1">进入下一题会结束本题；持仓按当前已揭示市值结算，待成交委托取消；账户继续记录。</p>}
            {v.pending && <p className="text-xs text-amber-400 mt-1">{v.pending.alias} 的{LABEL[v.pending.action]}待次日开盘执行，推进后查看成交结果。</p>}
            <details className="text-xs text-muted mt-1">
              <summary className="cursor-pointer">{PRACTICE[v.practice_kind]} · {v.strategies.map(s => s.name).join('、') || v.selection.name} · 训练详情</summary>
              <div className="space-y-1 mt-1">
                <p>{MODE[v.mode]}</p>
                {v.notices.map((notice, i) => <p key={i}>{notice}</p>)}
                {v.scan_info && <p>最近 T+{v.scan_info.day} 匹配池 {v.scan_info.eligible_size ?? v.scan_info.pool_size} 只 · 载入 {v.scan_info.selected.length} 只 / 设置上限 {v.candidate_count} 只{v.practice_kind === 'screening' && v.auto_scan ? ` · 每 ${v.scan_interval_min}–${v.scan_interval_max} 个交易日重选` : ''}</p>}
                {v.selection.type === 'strategy' && <p>来源：{v.strategy_source === 'cached' ? '导入信号日抽题' : '随机日期逐日查询信号'}</p>}
              </div>
            </details>
          </div>
        </section>
        <section className={`${box} space-y-3`}>
          <h2 className="font-medium">记录判断 · {alias || '未选择候选'}</h2>
          <p className="text-xs text-muted">每只股票独立保存草稿，切换、推进或刷新后恢复。提交观察后加入本场记录。</p>
          <label className="block text-sm">操作模式（可选）<input aria-label="操作模式" className={`${field} mt-1`} maxLength={80} disabled={!active || busy} value={pattern} onChange={e => editDraft({ pattern: e.target.value })} placeholder="例如：短平台突破、均线修复" /></label>
          <label className="block text-sm">观察、买卖或放弃理由<textarea aria-label="判断理由" className={`${field} mt-1 h-28`} disabled={!active || busy} maxLength={3000} value={reason} onChange={e => editDraft({ reason: e.target.value })} placeholder="我看到了什么？为什么现在行动？" /></label>
          <div className="flex flex-wrap gap-1">{TAGS.map(tag => <button key={tag} type="button" disabled={!active || busy} className={`rounded px-2 py-1 text-xs border ${tags.includes(tag) ? 'border-blue-400 text-blue-400' : 'border-border text-muted'}`} onClick={() => editDraft({ tags: tags.includes(tag) ? tags.filter(t => t !== tag) : [...tags, tag] })}>{tag}</button>)}</div>
          <label className="block text-sm">什么变化会使判断失效<textarea aria-label="失效条件" className={`${field} mt-1 h-16`} maxLength={1000} disabled={!active || busy} value={invalidation} onChange={e => editDraft({ invalidation: e.target.value })} placeholder="例如跌回平台、量价背离…" /></label>
          <div className="grid grid-cols-2 gap-2"><label className="text-sm">信心<select aria-label="信心" className={field} disabled={!active || busy} value={confidence} onChange={e => editDraft({ confidence: Number(e.target.value) })}>{[1, 2, 3, 4, 5].map(n => <option key={n} value={n}>{n}/5</option>)}</select></label>
            <label className="text-sm">使用现金比例<select aria-label="买入比例" className={field} disabled={!active || busy} value={size} onChange={e => editDraft({ size: Number(e.target.value) })}>{[.25, .5, 1].map(n => <option key={n} value={n}>{n * 100}%</option>)}</select></label></div>
          <button className={`${btn} w-full`} disabled={!active || busy || !selected} onClick={() => decide('note')}>记录观察</button>
          <div className="grid grid-cols-2 gap-2"><button className={btn} disabled={!active || busy || !selected?.tradable || !!v.position || !!v.pending} onClick={() => decide('buy')}>记录并买入</button>
            <button className={btn} disabled={!active || busy || v.position?.alias !== alias || !!v.pending || (v.mode === 'close_t' && v.cursor <= v.position.entry_day)} onClick={() => decide('sell')}>记录并卖出</button>
            <button className={btn} disabled={!active || busy || v.position?.alias !== alias} onClick={() => decide('hold')}>继续持有</button>
            <button className={btn} disabled={!active || busy || !selected || selected.held} onClick={() => decide('reject')}>放弃候选</button></div>
          {v.pending && <button className={btn} disabled={!active || busy || alias !== v.pending.alias} onClick={() => decide('cancel')}>记录理由并取消 {v.pending.alias} 的委托</button>}
          <p className="text-xs text-muted">每次判断保存原理由、指标选择和当时已加载的 K 线；全部前史按本地数据覆盖范围加载，原有判断记录保留。</p>
        </section>
      </div>

      <TrainingAccountPanel compact data={accountQuery.data} loading={accountQuery.isLoading} error={accountQuery.error?.message}
        busy={busy} onStop={() => stopAccount.mutate()} onSelect={id => navigate(`/training/account?account=${encodeURIComponent(id)}${verification ? '&verify=1' : ''}`)} onResume={openSession} />
      <details className={box}><summary className="cursor-pointer font-medium">本题操作记录与复盘 · 展开查看</summary><div className="mt-3 space-y-4">
      <section><div className="flex flex-wrap items-center gap-3 mb-2"><h2 className="font-medium">操作记录{onlySelectedRecords ? ` · ${alias || '未选择候选'}` : ' · 全部股票'}</h2>
        <label className="text-sm flex items-center gap-1"><input type="checkbox" checked={onlySelectedRecords} onChange={e => setOnlySelectedRecords(e.target.checked)} />只看当前股票记录</label></div>
        {!v.events.some(e => !onlySelectedRecords || e.alias === alias) && <p className="text-sm text-muted">{onlySelectedRecords ? '当前股票尚无已提交记录，可填写判断后点击“记录观察”。' : '尚无判断记录。'}</p>}
        <div className="max-h-72 overflow-y-auto space-y-2">{[...v.events].reverse().filter(e => !onlySelectedRecords || e.alias === alias).map(e => <div key={e.id} className="border-b border-border pb-2 text-sm break-words"><strong>{e.id} · T+{e.day} · {e.alias} · {LABEL[e.action]}{e.pattern ? ` · ${e.pattern}` : ''}</strong><span className="text-xs text-muted ml-2">{e.outcome}</span><p className="whitespace-pre-wrap mt-1">{e.reason}</p>
          {!!e.tags.length && <p className="text-xs text-muted mt-1">标签：{e.tags.join('、')}</p>}
          {e.invalidation && <p className="text-xs text-muted whitespace-pre-wrap mt-1">失效条件：{e.invalidation}</p>}
          <p className="text-xs text-muted mt-1">信心 {e.confidence ?? 3}/5 · 使用现金比例 {((e.size ?? .5) * 100).toFixed(0)}%</p>
        </div>)}</div>
      </section>
      {v.review && <section className={`${box} space-y-3`}><h2 className="font-medium">本场复盘 · {v.review.start} 至 {v.review.end}</h2>
        <p className="text-sm">主动平仓 {v.review.stats.trades} 笔，胜率 {pct(v.review.stats.win_rate)}，本题收益 {pct(v.review.stats.total_return)}，最大回撤 {pct(v.review.stats.max_drawdown)}。{v.account_settlement ? '本题原持仓已按市值结算到持续账户，不计主动平仓胜率。' : `未平仓 ${v.review.stats.open_positions} 笔按市值保留，不计胜率。`}</p>
        <div className="flex flex-wrap gap-2 text-sm">{v.review.identities.map(i => <span key={i.alias} className="rounded bg-elevated px-2 py-1">{i.alias} = {i.symbol} {i.name}</span>)}</div>
        {!!v.review.future_outcomes?.length && <div id="training-outcomes" className="space-y-2 border-t border-border pt-3">
          <h3 className="font-medium">股票后续表现 · T+0 至 T+{v.horizon}</h3>
          <p className="text-xs text-muted">以 T+0 收盘价为基准，最高／最低价统计 T+1 至预设最后训练日 {v.review.planned_end}；提前结束也统计后续行情，未出现上涨／下跌时记为 0。以下为股票涨跌幅，账户收益按你的实际操作单独计算。</p>
          <div className="overflow-x-auto"><table className="w-full text-sm whitespace-nowrap">
            <thead><tr className="text-left text-muted border-b border-border"><th className="py-2 pr-4">股票</th><th className="pr-4">起始收盘</th><th className="pr-4">最大涨幅</th><th className="pr-4">最大跌幅</th><th className="pr-4">最终涨跌幅</th><th className="pr-4">最后收盘</th><th>操作</th></tr></thead>
            <tbody>{v.review.future_outcomes.map(result => <tr key={result.alias} className="border-b border-border align-top">
              <td className="py-2 pr-4">{result.alias} · {v.review?.identities.find(i => i.alias === result.alias)?.name}</td>
              <td className="py-2 pr-4">{result.base_close?.toFixed(2) ?? '缺少T+0行情'}<span className="block text-xs text-muted">{result.base_date}</span></td>
              <td className="py-2 pr-4 text-red-400">{pct(result.max_rise)}{result.max_rise_day != null && <span className="block text-xs text-muted">T+{result.max_rise_day} · {result.max_rise_price?.toFixed(2)} 元</span>}</td>
              <td className="py-2 pr-4 text-emerald-400">{pct(result.max_fall)}{result.max_fall_day != null && <span className="block text-xs text-muted">T+{result.max_fall_day} · {result.max_fall_price?.toFixed(2)} 元</span>}</td>
              <td className={`py-2 pr-4 ${(result.final_return ?? 0) >= 0 ? 'text-red-400' : 'text-emerald-400'}`}>{pct(result.final_return)}</td>
              <td className="py-2 pr-4">{result.final_close?.toFixed(2) ?? '—'}{result.last_available_day != null && <span className="block text-xs text-muted">T+{result.last_available_day}</span>}{result.data_status !== 'complete' && <span className="block text-xs text-amber-400">{result.data_status === 'missing_start' ? '缺少基准，无法统计' : result.data_status === 'partial_prices' ? '部分最高／最低价缺失' : '按最后有行情日统计'}</span>}</td>
              <td className="py-2"><button className={btn} disabled={busy || conflict} onClick={() => replay.mutate(result.alias)}>重训 {result.alias}</button></td>
            </tr>)}</tbody>
          </table></div>
          <p className="text-xs text-muted">价格采用本题冻结的前复权口径。重训保留原题和原理由，复制同一只股票及同一区间从 T+0 重新开始；已看结果的重训按策略另行统计。</p>
        </div>}
        <div className="flex flex-wrap gap-2"><button className={btn} disabled={busy || conflict} onClick={() => nextQuestion.mutate()}>{nextQuestion.isPending ? '正在准备下一题…' : '沿用本题设置 · 下一题 →'}</button><button className={btn} disabled={download.isPending} onClick={() => download.mutate()}>导出原始理由与图表记录</button><button className={btn} disabled={analyze.isPending} onClick={() => analyze.mutate()}>{analyze.isPending ? '正在总结判断逻辑…' : 'AI 总结本场判断'}</button></div>
        {Boolean(aiText || v.review.analyses?.length) && <div className="text-sm break-words border-t border-border pt-3"><MarkdownRenderer content={aiText || v.review.analyses?.[v.review.analyses.length - 1]?.content || ''} /></div>}
      </section>}
      </div></details>
    </>}
    {!v && <TrainingAccountPanel compact data={accountQuery.data} loading={accountQuery.isLoading} error={accountQuery.error?.message}
      busy={busy} onStop={() => stopAccount.mutate()} onSelect={id => navigate(`/training/account?account=${encodeURIComponent(id)}${verification ? '&verify=1' : ''}`)} onResume={openSession} />}
    <details className={box} onToggle={e => setExtrasOpen(e.currentTarget.open)}><summary className="cursor-pointer font-medium">训练汇总与历史归档 · 展开查看</summary><div className="mt-3 space-y-4">
    <section><h2 className="font-medium mb-2">训练汇总</h2>
      {summary.error && <p className="text-red-400 text-sm">{summary.error.message}</p>}
      {!summary.data?.groups.length && <p className="text-sm text-muted">结束第一题后出现统计；同一策略的训练自动归在一起，训练方式与成交口径分别统计。</p>}
      {summary.data?.groups.map(g => <div key={JSON.stringify([g.record_group?.id, g.mode, g.selection_type, g.practice_kind, g.is_replay])} className="text-sm border-b border-border py-2"><strong>{g.is_replay ? '重训（已看结果） · ' : ''}{g.record_group?.name || g.selection_name} · {PRACTICE[g.practice_kind]} · {MODE[g.mode]}</strong><p>{g.sessions} 场 · {g.trades} 笔主动平仓 · 胜率 {pct(g.win_rate)} · 平均每笔 {pct(g.mean_trade_return)} · 未平仓 {g.open_positions}</p><p className="text-xs text-muted">买入标签：{Object.entries(g.buy_tags).map(([tag, n]) => `${tag} ${n} 次`).join('、') || '尚无标签'}</p></div>)}
      <p className="text-xs text-muted mt-2">{summary.data?.note}</p>
    </section>
    <section className={box}><h2 className="font-medium mb-2">历史训练与恢复 · 按策略归档</h2>
      <p className="text-xs text-muted mb-3">同一策略自动归在一起；展开可查看复盘或继续未结束的训练。多策略组合单独归组。</p>
      {sessions.error && <p className="text-red-400 text-sm">{sessions.error.message}</p>}
      {!recordGroups.length && <p className="text-sm text-muted">尚无训练场次。</p>}
      <div className="max-h-96 overflow-y-auto space-y-3">{recordGroups.map(group => <details key={group.id} open={group.items.some(s => s.id === sessionId)} className="rounded border border-border p-3">
        <summary className="cursor-pointer break-words"><strong>{group.verification ? '功能验证 · ' : ''}{group.name}</strong><span className="text-xs text-muted ml-2">{group.items.length} 题 · 已结束 {group.items.filter(s => s.finished).length} · 进行中 {group.items.filter(s => !s.finished).length}</span></summary>
        <div className="space-y-2 mt-3">{group.items.map((s, i) => <button className={`${btn} block w-full text-left whitespace-normal ${s.id === sessionId ? 'border-blue-400 text-blue-400' : ''}`} key={s.id} disabled={busy} onClick={() => openSession(s.id)}>记录 {i + 1} · 第 {s.question_number} 题{s.replay_number ? ` · 重训第 ${s.replay_number} 次` : ''} · {PRACTICE[s.practice_kind]} · {MODE[s.mode]} · T+{s.cursor} · {s.finished ? '查看复盘' : '继续训练'} · {pct(s.total_return)}</button>)}</div>
      </details>)}</div>
    </section>
    </div></details>
  </div>
}
