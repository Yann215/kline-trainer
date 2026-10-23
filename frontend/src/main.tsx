import React, { useState } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import TrainingPage from './custom/training/TrainingPage'
import TrainingAccountPage from './custom/training/TrainingAccountPage'
import { setTheme, useTheme } from './lib/theme'
import './index.css'

const queryClient = new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } } })
class ErrorBoundary extends React.Component<React.PropsWithChildren, { error: string }> {
  state = { error: '' }
  static getDerivedStateFromError(error: Error) { return { error: error.message } }
  render() {
    return this.state.error ? <div role="alert" className="p-8"><h2>页面未能加载</h2><p>{this.state.error}</p><button className="mt-4 border p-2" onClick={() => location.reload()}>刷新页面</button></div> : this.props.children
  }
}
function App() {
  const theme = useTheme()
  const [help, setHelp] = useState(false)
  const dataset = useQuery({ queryKey: ['dataset'], queryFn: async () => {
    const response = await fetch('/api/health')
    if (!response.ok) throw new Error('无法连接训练服务')
    return response.json() as Promise<{ dataset: string; price_mode: string; symbols: number; start: string; end: string }>
  }, staleTime: Infinity })
  return <div className="min-h-screen">
    <header className="flex flex-wrap items-center gap-4 border-b px-5 py-3 bg-surface">
      <NavLink to="/training" className="font-semibold tracking-wide">KLINE TRAINER</NavLink>
      <nav className="flex items-center gap-4 text-sm text-muted"><NavLink to="/training">盘感训练</NavLink><NavLink to="/training/account">训练账户</NavLink></nav>
      <div className="ml-auto flex gap-4 text-xs"><button onClick={() => setHelp(!help)}>数据与使用说明</button><button aria-label="切换主题" onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '浅色' : '深色'}</button></div>
    </header>
    {dataset.error && <p role="alert" className="m-4 text-red-400">{dataset.error.message}</p>}
    {dataset.data && <div className="border-b px-5 py-2 text-xs text-muted">当前数据：{dataset.data.dataset} · {dataset.data.symbols} 个标的 · {dataset.data.price_mode === 'raw' ? '合成演示价格' : '前复权价格'}{dataset.data.dataset.includes('合成') && ' · 演示行情为程序生成，非真实股票行情'}</div>}
    {help && <aside className="m-5 rounded-card border bg-surface p-4 text-sm space-y-2"><p>先选择训练类型并开始。观察匿名K线，为每只候选独立记录判断；买卖、下一交易日、结束揭示均在图表下方。</p><p>结束本题时持仓按市值结算，资金延续到下一题。“结束记录”才关闭持续账户。账户详情可导出记录。</p><p>首次启动自动生成演示行情。导入真实行情：在终端运行 <code>kline-trainer import-csv bars.csv --signals signals.csv</code>；本地 TSP 可用 <code>import-tsp</code>，格式和命令见 README。</p><p>数据仅保存在本机。AI复盘为可选功能，配置后点击分析会发送操作理由与当时匿名快照摘要。</p></aside>}
    <main className="mx-auto max-w-[1920px]"><Routes><Route path="/training" element={<TrainingPage />} /><Route path="/training/account" element={<TrainingAccountPage />} /><Route path="*" element={<Navigate to="/training" replace />} /></Routes></main>
  </div>
}
ReactDOM.createRoot(document.getElementById('root')!).render(<ErrorBoundary><QueryClientProvider client={queryClient}><BrowserRouter><App /></BrowserRouter></QueryClientProvider></ErrorBoundary>)
