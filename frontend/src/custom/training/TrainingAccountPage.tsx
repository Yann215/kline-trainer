import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { TrainingAccountPanel } from './TrainingAccountPanel'

export default function TrainingAccountPage() {
  const [params, setParams] = useSearchParams()
  const verification = params.get('verify') === '1'
  const accountId = params.get('account') ?? ''
  const navigate = useNavigate()
  const qc = useQueryClient()
  const account = useQuery({ queryKey: QK.trainingAccount(verification, accountId),
    queryFn: () => api.trainingAccount(verification, accountId), retry: false })
  const stop = useMutation({ mutationFn: () => {
    const current = account.data?.account
    if (!current) throw new Error('尚无持续账户')
    return api.trainingAccountStop(current.id, { revision: current.revision, request_id: crypto.randomUUID() })
  }, onSuccess: data => {
    qc.setQueryData(QK.trainingAccount(verification, accountId), data)
    void qc.invalidateQueries({ queryKey: QK.trainingAccount(verification) })
    if (data.account) {
      void qc.invalidateQueries({ queryKey: QK.trainingAccount(verification, data.account.id) })
      void qc.invalidateQueries({ queryKey: QK.trainingAccountRecords(data.account.id) })
      void qc.invalidateQueries({ queryKey: QK.trainingSession(data.account.current_session_id) })
    }
    void qc.invalidateQueries({ queryKey: QK.trainingSessions })
    void qc.invalidateQueries({ queryKey: QK.trainingSummary })
  }, onError: () => { void account.refetch() } })
  return <div className="space-y-4 p-4 md:p-6 min-w-0">
    <div className="flex flex-wrap justify-between items-center gap-3">
      <div><h1 className="text-xl font-semibold">训练账户详情</h1><p className="text-sm text-muted mt-1">查看跨题收益、训练题目与原始判断；账户资金和记录持续保存。</p></div>
      <Link className="text-sm text-blue-400" to={`/training${verification ? '?verify=1' : ''}`}>返回盘感训练 →</Link>
    </div>
    <TrainingAccountPanel data={account.data} loading={account.isLoading}
      error={account.error?.message || stop.error?.message} busy={stop.isPending}
      onStop={() => stop.mutate()}
      onSelect={id => setParams({ account: id, ...(verification ? { verify: '1' } : {}) })}
      onResume={id => navigate(`/training?${new URLSearchParams({ session: id, ...(verification ? { verify: '1' } : {}) })}`)} />
    {account.isError && <button className="rounded-btn border border-border px-3 py-2 text-sm" onClick={() => void account.refetch()}>重新读取账户</button>}
  </div>
}
