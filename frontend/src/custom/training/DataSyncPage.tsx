import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

type Dataset = { dataset: string; symbols: number; start: string; end: string }
type SyncStatus = {
  source: string
  status: 'idle' | 'running' | 'complete' | 'failed'
  message: string
  result: { symbols: number; start: string; end: string } | null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const body = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败 (${response.status})`)
  return body as T
}

export default function DataSyncPage() {
  const qc = useQueryClient()
  const [source, setSource] = useState('')
  const [edited, setEdited] = useState(false)
  const dataset = useQuery({ queryKey: ['dataset'], queryFn: () => request<Dataset>('/api/health') })
  const status = useQuery({
    queryKey: ['data-sync'], queryFn: () => request<SyncStatus>('/api/data-sync'),
    refetchInterval: query => query.state.data?.status === 'running' ? 2000 : false,
  })
  useEffect(() => { if (!edited && status.data?.source) setSource(status.data.source) }, [status.data?.source, edited])
  const sync = useMutation({
    mutationFn: () => request<SyncStatus>('/api/data-sync', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source }),
    }),
    onSuccess: data => { qc.setQueryData(['data-sync'], data); setEdited(false) },
  })
  const job = status.data
  return <section className="mx-auto max-w-3xl space-y-5 p-5">
    <div><h1 className="text-xl font-semibold">数据同步</h1><p className="mt-2 text-sm text-muted">从本机 TSP 行情目录复制历史数据，用于盘感训练。</p></div>
    <div className="rounded-card border border-border bg-surface p-4 text-sm space-y-2">
      <h2 className="font-semibold">当前正在使用</h2>
      {dataset.data ? <p>{dataset.data.dataset} · {dataset.data.symbols} 个标的 · {dataset.data.start} 至 {dataset.data.end}</p> : <p>正在读取当前数据…</p>}
      <p className="text-muted">同步读取本地已有行情，不会联网下载或更新 TSP 数据；如需最新行情，请先在 TSP 中更新。</p>
    </div>
    <div className="rounded-card border border-border bg-surface p-4 text-sm space-y-3">
      <label htmlFor="sync-source" className="block font-semibold">TSP 行情数据目录</label>
      <input id="sync-source" className="w-full rounded-btn border border-border bg-elevated px-3 py-2" value={source}
        onChange={e => { setEdited(true); setSource(e.target.value) }} placeholder="例如 D:\\股票工具\\data" />
      <p className="text-muted">填写 TSP 的 data 文件夹完整路径。程序仅读取行情，训练账户和历史记录会保留。</p>
      <button className="rounded-btn border border-border bg-elevated px-4 py-2 hover:bg-border disabled:cursor-not-allowed disabled:opacity-40"
        disabled={!source.trim() || sync.isPending || job?.status === 'running'} onClick={() => sync.mutate()}>开始同步</button>
      {(sync.error || status.error) && <p role="alert" className="text-red-400">{(sync.error || status.error)?.message}</p>}
      {job && job.status !== 'idle' && <div role="status" className="rounded-btn border border-border p-3">
        <p>{job.status === 'running' ? '同步中：' : job.status === 'failed' ? '同步失败：' : ''}{job.message}</p>
        {job.result && <p className="mt-1">新数据：{job.result.symbols} 个标的 · {job.result.start} 至 {job.result.end}</p>}
        {job.status === 'complete' && <p className="mt-2 text-muted">正在训练的题目不会中断。关闭窗口不会停止后台服务，重启电脑后新数据会自动生效。</p>}
      </div>}
    </div>
  </section>
}
