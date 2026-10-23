import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import ReactECharts from 'echarts-for-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { accountMaxDrawdown } from '@/lib/training-account'
import type { TrainingAccountData } from '@/lib/training-types'

const money = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const pct = (value: number | null | undefined) => value == null ? '—' : `${(value * 100).toFixed(2)}%`
const button = 'rounded-btn border border-border bg-elevated px-3 py-2 text-sm disabled:opacity-40'
const labels: Record<string, string> = { buy: '买入', sell: '卖出', note: '观察', hold: '持有', reject: '放弃', cancel: '取消委托' }

export function TrainingAccountPanel({ data, loading, error, busy, onStop, onSelect, onResume, compact = false }: {
  compact?: boolean; data?: TrainingAccountData; loading: boolean; error?: string; busy: boolean
  onStop: () => void; onSelect: (id: string) => void; onResume: (id: string) => void
}) {
  const account = data?.account
  const maxDrawdown = useMemo(() => accountMaxDrawdown(data?.points ?? [], account?.initial ?? 100000),
    [data?.points, account?.initial])
  const [pattern, setPattern] = useState('')
  const [detailsOpen, setDetailsOpen] = useState(!compact)
  useEffect(() => setPattern(''), [account?.id])
  const records = useQuery({ queryKey: QK.trainingAccountRecords(account?.id ?? ''),
    queryFn: () => api.trainingAccountRecords(account!.id), enabled: !!account?.id && detailsOpen })
  const patterns = [...new Set((records.data?.events ?? []).map(e => e.pattern || '未标注模式'))]
  const download = useMutation({ mutationFn: () => api.trainingAccountExport(account!.id), onSuccess: payload => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `模式操作账户_${account!.id.slice(0, 8)}.json`; anchor.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } })
  const option = useMemo(() => {
    const points = data?.points ?? []
    const initial = account?.initial ?? 100000
    return {
      backgroundColor: 'transparent',
      grid: { left: 58, right: 22, top: 22, bottom: 60 },
      tooltip: { trigger: 'axis', formatter: (items: unknown) => {
        const list = items as { dataIndex: number }[]
        const point = points[list?.[0]?.dataIndex ?? 0]
        if (!point) return ''
        return `${point.question ? `第${point.question}题 · T+${point.day}` : '账户起点'}<br/>净值 ${money(point.value)} 元<br/>累计收益 ${pct(point.value / initial - 1)}`
      } },
      xAxis: { type: 'category', boundaryGap: false,
        data: points.map(point => point.question ? `${point.question}题 T+${point.day}` : '起点'),
        axisLabel: { color: '#9396a5', hideOverlap: true }, axisLine: { lineStyle: { color: '#454854' } } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: '#9396a5', formatter: '{value}%' },
        splitLine: { lineStyle: { color: '#454854', opacity: .35 } } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 6 }],
      series: [{ name: '账户累计收益', type: 'line', showSymbol: points.length < 3, symbolSize: 6,
        data: points.map(point => Number(((point.value / initial - 1) * 100).toFixed(6))),
        lineStyle: { color: '#60a5fa', width: 2 }, itemStyle: { color: '#60a5fa' },
        areaStyle: { color: 'rgba(96,165,250,.12)' },
        markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: '#9396a5', type: 'dashed' }, data: [{ yAxis: 0 }] } }],
    }
  }, [data?.points, account?.initial])
  return <section id="training-account" className="rounded-card border border-border bg-surface p-4 space-y-3 min-w-0">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-medium">账户收益曲线</h2>{account?.verification && <p className="mt-1 text-xs text-amber-400">功能验证账户，不计入正式训练统计</p>}</div>
      {compact && <Link className="text-sm text-blue-400" to={`/training/account?${new URLSearchParams({ ...(account?.id ? { account: account.id } : {}), ...(account?.verification ? { verify: '1' } : {}) })}`}>打开账户详情 ↗</Link>}
      {!compact && !!data?.accounts.length && <select aria-label="账户记录" className="rounded-btn border border-border bg-surface px-2 py-2 text-sm max-w-full" value={account?.id ?? ''} onChange={e => onSelect(e.target.value)}>
        {data.accounts.map((item, i) => <option key={item.id} value={item.id}>{item.status === 'active' ? '记录中' : '已结束'} · {item.created_at.replace('T', ' ')} · 账户 {data.accounts.length - i}</option>)}
      </select>}
    </div>
    {loading && <p className="text-sm text-muted">正在恢复账户曲线…</p>}
    {error && <p role="alert" className="text-sm text-red-400">{error}</p>}
    {!loading && !account && <p className="text-sm text-muted">开始训练或在当前旧题首次操作后自动建立持续账户。旧的独立训练记录保留，不混入累计曲线。</p>}
    {account && <>
      <div role="img" aria-label="持续账户累计收益曲线"><ReactECharts option={option} notMerge style={{ height: 270, width: '100%' }} /></div>
      <details open={compact ? undefined : true} onToggle={e => setDetailsOpen(e.currentTarget.open)}>
        <summary className="cursor-pointer text-sm text-muted">账户统计与操作记录 · 展开查看</summary>
        <div className="mt-3 space-y-3">
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-2 text-sm">
        {[['初始资金', `${money(account.initial)} 元`], ['账户净值', `${money(account.equity)} 元`], ['累计收益', pct(account.total_return)],
          ['最大回撤', pct(maxDrawdown)], ['累计题数', `${account.questions} 题`]].map(([label, value]) => <div className="rounded bg-elevated p-3" key={label}><div className="text-xs text-muted">{label}</div><strong className="block mt-1 break-words">{value}</strong></div>)}
      </div>
      <p className="text-xs text-muted">横轴按训练先后排列，隐藏行情日期；曲线包含持仓浮动收益和实际训练费用。结束本题时按当前已揭示市值移交虚拟资金，不算主动卖出，不另收卖出费用，也不计入平仓胜率。重训会接续同一账户，模式统计另标“已看结果”。</p>
      <div className="flex flex-wrap items-center gap-2">
        {account.status === 'active' ? <><button className={button} disabled={busy} onClick={() => onResume(account.current_session_id)}>继续账户当前题</button><button className={`${button} border-red-400 text-red-400`} disabled={busy} onClick={onStop}>结束记录</button></> : <span className="text-sm text-muted">本账户已结束，记录与曲线保留；开始新训练会建立新账户。</span>}
        <button className={button} disabled={download.isPending} onClick={() => download.mutate()}>导出账户记录</button>
      </div>
      {download.error && <p className="text-sm text-red-400">{download.error.message}</p>}
      <div className="border-t border-border pt-3 space-y-2">
        <h3 className="font-medium">账户训练题目</h3>
        <p className="text-xs text-muted">按本账户的题目顺序查看；尚未提交判断的题目也会保留。</p>
        <div className="max-h-96 overflow-auto"><table className="w-full text-sm whitespace-nowrap">
          <thead><tr className="text-left text-muted border-b border-border"><th className="py-2 pr-4">题目</th><th className="pr-4">策略</th><th className="pr-4">进度</th><th className="pr-4">本题收益</th><th className="pr-4">结余净值</th><th>记录</th></tr></thead>
          <tbody>{records.data?.questions?.map(question => <tr className="border-b border-border" key={question.id}>
            <td className="py-2 pr-4">第 {question.question} 题{question.is_replay ? ' · 重训' : ''}</td>
            <td className="pr-4 max-w-64 whitespace-normal">{question.strategy}</td><td className="pr-4">T+{question.cursor} · {question.finished ? '已结束' : '进行中'}</td>
            <td className="pr-4">{pct(question.total_return)}</td><td className="pr-4">{money(question.equity)}</td>
            <td><button className={`${button} my-1`} disabled={busy} onClick={() => onResume(question.id)}>{question.id === account.current_session_id && account.status === 'active' ? '继续当前题' : '查看本题记录'}</button></td>
          </tr>)}</tbody>
        </table></div>
      </div>
      <div className="border-t border-border pt-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2"><h3 className="font-medium">按操作模式回顾</h3><select aria-label="筛选操作模式" className="rounded border border-border bg-surface p-2 text-sm max-w-full" value={pattern} onChange={e => setPattern(e.target.value)}><option value="">全部模式</option>{patterns.map(value => <option key={value}>{value}</option>)}</select></div>
        {records.isLoading && <p className="text-sm text-muted">正在读取模式记录…</p>}
        {records.error && <p className="text-sm text-red-400">{records.error.message}</p>}
        <p className="text-xs text-muted">主动平仓 {records.data?.stats.trades ?? 0} 笔 · 胜率 {pct(records.data?.stats.win_rate)} · 市值结算 {records.data?.stats.settlements ?? 0} 次。模式归属按买入时记录；已看结果的重训主动平仓 {records.data?.stats.replay_trades ?? 0} 笔。</p>
        {records.data?.groups.filter(group => !pattern || group.pattern === pattern).map(group => <div className="text-sm" key={`${group.pattern}:${group.is_replay}`}>{group.pattern}{group.is_replay ? '（重训，已看结果）' : ''}：{group.trades} 笔 · 胜率 {pct(group.win_rate)} · 已实现盈亏 {money(group.profit)} 元</div>)}
        {!records.data?.events.length && <p className="text-sm text-muted">尚无操作记录；在右侧填写操作模式和判断理由后提交即可保存。</p>}
        <div className="max-h-80 overflow-y-auto space-y-2">{[...(records.data?.events ?? [])].reverse().filter(event => !pattern || (event.pattern || '未标注模式') === pattern).map(event => <div className="border-b border-border pb-2 text-sm break-words" key={`${event.session_id}:${event.id}`}>
          <strong>第{event.question}题 · T+{event.day} · {event.alias} · {labels[event.action] ?? event.action} · {event.pattern || '未标注模式'}{event.is_replay ? ' · 已看结果' : ''}</strong>
          <p className="whitespace-pre-wrap mt-1">{event.reason}</p><p className="text-xs text-muted mt-1">{event.outcome}</p>
          {event.invalidation && <p className="text-xs text-muted whitespace-pre-wrap mt-1">失效条件：{event.invalidation}</p>}
          <p className="text-xs text-muted mt-1">信心 {event.confidence ?? 3}/5 · 使用现金 {((event.size ?? .5) * 100).toFixed(0)}%{event.tags?.length ? ` · ${event.tags.join('、')}` : ''}</p>
          <button className="text-xs text-blue-400 mt-1" disabled={busy} onClick={() => onResume(event.session_id)}>查看对应题目 →</button>
        </div>)}</div>
        {!!records.data?.omitted_events && <p className="text-xs text-muted">此处展示最近200条，共{records.data.total_events}条；完整记录保存在账户导出中。</p>}
      </div>
        </div>
      </details>
    </>}
  </section>
}
