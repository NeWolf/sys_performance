import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, apiResponse, errorMessage, formatMemory, formatTime, sessionPath, useResource } from './api'
import type { Session } from './api'
import { Dashboard } from './Dashboard'
import { Compare } from './Compare'
import { Devices } from './Devices'
import { EditContext, useEditGuard, useWorkspaceGuard } from './Editing'
import { ViewPanel } from './ViewPanel'
import './App.css'

function createdLabel(value: string | number) {
  // 后端返回带时区的 ISO 时间字符串；数字时间统一视为毫秒。
  return formatTime(typeof value === 'string' ? Date.parse(value) : value)
}

export default function App() {
  const guard = useWorkspaceGuard()
  return <EditContext.Provider value={guard}><Workspace /></EditContext.Provider>
}

const workspaceViews = [
  { id: 'capture', label: '数据集', description: 'ADB 设备采集 · 本地日志导入' },
  { id: 'analysis', label: '性能分析', description: '系统资源、进程排行、趋势叠加与精确合并' },
  { id: 'compare', label: '双次采集对比', description: 'CPU、内存与 IO 差异 · 进程变化排行' },
  { id: 'report', label: '配置与导出', description: '分析配置与离线交互 HTML 报告下载' },
  { id: 'advanced', label: '高级功能', description: '采样间隔与采集写入选项' },
] as const
type WorkspaceView = typeof workspaceViews[number]['id']

function Workspace() {
  const [view, setView] = useState<WorkspaceView>('capture')
  const currentView = workspaceViews.find((item) => item.id === view)!
  const guard = useEditGuard()
  const [revision, setRevision] = useState(0)
  const sessions = useResource<Session[]>('/api/sessions', revision)
  const [compareSessions, setCompareSessions] = useState<Session[]>()
  // 列表刷新不卸载对比页面；成功返回后仍同步删除、新增的会话。
  if (sessions.data && sessions.data !== compareSessions) setCompareSessions(sessions.data)
  const [selected, setSelected] = useState<string | null>(null)
  const activeId = selected ?? sessions.data?.[0]?.id ?? null
  const active = sessions.data?.find((session) => session.id === activeId)
  // 首次列表就绪时固定选择；刷新期间保留当前会话，不通过 effect 追加渲染。
  if (selected === null && activeId) setSelected(activeId)
  const [files, setFiles] = useState<File[]>([])
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)
  const operation = useRef<AbortController | null>(null)
  useEffect(() => () => operation.current?.abort(), [])

  async function perform(label: string, action: (signal: AbortSignal) => Promise<void>) {
    if (operation.current || !guard.acquire('workspace-operation')) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(label)
    setError('')
    setNotice('')
    try { await action(controller.signal) }
    catch (failure) { if (!controller.signal.aborted) setError(errorMessage(failure)) }
    finally {
      operation.current = null
      guard.update('workspace-operation')
      if (!controller.signal.aborted) setBusy('')
    }
  }

  function importFiles(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!files.length) return
    if (files.length > 5) {
      setError('每次最多导入 5 个日志文件')
      return
    }
    const oversized = files.find((file) => file.size > 1024 * 1024 * 1024)
    if (oversized) {
      setError(`文件“${oversized.name}”超过单文件 1 GB（1024 MB）限制`)
      return
    }
    if (!guard.confirm('导入成功后将切换会话并放弃未保存配置，是否继续？')) return
    void perform('正在本地导入并解析…', async (signal) => {
      const form = new FormData()
      files.forEach((file) => form.append('files', file))
      form.append('title', title.trim() || files[0]!.name)
      const result = await api<{ id: string; duplicate: boolean }>('/api/import', { method: 'POST', body: form, signal })
      if (signal.aborted) return
      setSelected(result.id)
      setView('analysis')
      setRevision((value) => value + 1)
      setNotice(result.duplicate ? '这些日志已导入，已打开已有会话。' : '导入完成，日志已交由本机服务解析。')
      setFiles([])
      setTitle('')
      if (fileInput.current) fileInput.current.value = ''
    })
  }

  function deleteSession() {
    if (!activeId || !guard.confirm() || !window.confirm(`确定删除“${active?.name || '当前会话'}”？此操作会删除本地分析数据，无法撤销；原始日志文件不受影响。`)) return
    void perform('正在删除会话…', async (signal) => {
      const result = await api<{ deleted: boolean }>(sessionPath(activeId), { method: 'DELETE', signal })
      if (!result.deleted) throw new Error('本地服务未确认删除，请刷新后核实。')
      if (signal.aborted) return
      setSelected(null)
      setRevision((value) => value + 1)
      setNotice('会话已删除，原始日志文件未改动。')
    })
  }

  function exportReport() {
    if (!activeId || !guard.confirm('有未保存的分析配置或进程组。离线交互报告直接取原始数据，不包含人工配置或进程组；下载不会保存或丢弃当前草稿。是否继续下载？')) return
    void perform('正在生成 HTML 报告…', async (signal) => {
      const response = await apiResponse(`${sessionPath(activeId)}/report`, { signal })
      const blob = await response.blob()
      if (signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      // 下载名必须去除控制字符及跨平台文件名禁用字符。
      // eslint-disable-next-line no-control-regex
      link.download = `${(active?.name || 'sysmonitor-report').replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 100)}.html`
      document.body.append(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
      setNotice('HTML 报告已生成并触发浏览器下载。')
    })
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><img className="brand-mark" src="/tj.png" alt="sysmonitor 标志" /><div><strong>sysmonitor</strong><small>Android 性能分析</small></div></div>
        <div className="workspace-label">本地工作空间 <span className="status-dot" /> 离线分析</div>
        <nav className="workspace-nav" aria-label="工作区功能">{workspaceViews.map((item) => (
          <button key={item.id} className={view === item.id ? 'active' : ''} aria-current={view === item.id ? 'page' : undefined} onClick={() => setView(item.id)}><strong>{item.label}</strong><small>{item.description}</small></button>
        ))}</nav>
        <div className="history-heading"><h2>历史会话</h2><button className="text-button" disabled={guard.busy || sessions.loading} onClick={() => { if (guard.confirm()) setRevision((value) => value + 1) }}>刷新</button></div>
        {sessions.loading && <p className="sidebar-hint" role="status">正在读取本地历史…</p>}
        {sessions.error && <p className="sidebar-hint error" role="alert">{sessions.error}</p>}
        {sessions.data?.length === 0 && <p className="sidebar-hint">还没有会话，导入日志后将在此保留。</p>}
        <nav className="session-list" aria-label="历史分析会话">{sessions.data?.map((session) => (
          <button key={session.id} className={`session-item ${activeId === session.id ? 'active' : ''}`} disabled={guard.busy} aria-current={activeId === session.id ? 'page' : undefined} onClick={() => { if (activeId === session.id || !guard.confirm()) return; setSelected(session.id); setError(''); setNotice('') }}>
            <strong>{session.name}</strong><small>{createdLabel(session.created)}</small><span>{session.summary.files.length} 个文件 · {session.summary.cycles} 个周期</span>
          </button>
        ))}</nav>
        <div className="sidebar-footer">日志留在本机<br />离线导入 / 趋势分析 / HTML 报告</div>
      </aside>
      <main>
        <header className="topbar"><span>性能工作台 <b>/</b> {currentView.label}</span><span className="badge">本地服务 · 127.0.0.1</span></header>
        <div className="main-content">
          <div className="page-heading"><div><p className="eyebrow">ANDROID / {currentView.label}</p><h1>{currentView.label}</h1><p>{currentView.description}{active && (view === 'analysis' || view === 'report') ? ` · 当前会话：${active.name}` : ''}</p></div>
            <div className="actions">{view === 'report' && <button className="primary" disabled={!activeId || guard.busy} onClick={exportReport}>导出离线交互 HTML 报告</button>}{(view === 'analysis' || view === 'report') && <button className="danger" disabled={!activeId || guard.busy} onClick={deleteSession}>删除会话</button>}</div>
          </div>
          <Devices view={view} onAdvanced={() => setView('advanced')} disabled={guard.busy} onImported={(id) => { setSelected(id); setRevision((value) => value + 1); setView('analysis') }} />
          <ViewPanel active={view === 'capture'}>
            <details className="panel local-import">
              <summary>导入本地日志 <span>已有日志文件时展开</span></summary>
            <form className="import-form capture-import" onSubmit={importFiles}>
              <h2>导入本地日志</h2><p>每次最多 5 个文件，单文件最大 1 GB，仅发送给本地分析服务。大文件解析需要较长时间及足够磁盘空间。</p>
              <label className="file-picker">选择日志文件<input ref={fileInput} type="file" multiple disabled={guard.busy} onChange={(event) => setFiles(Array.from(event.target.files || []))} /></label>
              {files.length > 0 && <div className="file-selection"><strong>已选择 {files.length} 个文件</strong><ul>{files.map((file, index) => <li key={index} title={file.name}>{file.name} · {formatMemory(file.size / 1024)}</li>)}</ul></div>}
              <label htmlFor="session-title">会话名称（可选）</label>
              <input id="session-title" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={160} placeholder="例如：冷启动 · 第一次采集" disabled={guard.busy} />
              <button className="primary" type="submit" disabled={!files.length || guard.busy}>{busy.startsWith('正在本地') ? '正在解析…' : '导入并分析'}</button>
            </form>
            </details>
          </ViewPanel>
          {guard.dirty && <div className="banner warning" role="status">有未保存的进程组或人工分析配置，请在对应表单保存以保留用于分析。离线交互报告直接取原始数据，不包含人工配置或进程组；下载不会保存或丢弃草稿。</div>}
          {busy &&<div className="banner info" role="status">{busy} 请勿关闭页面。</div>}
          {error && <div className="banner error" role="alert">{error}</div>}
          {notice && <div className="banner success" role="status">{notice}</div>}
          {view === 'compare' && (compareSessions ? <Compare sessions={compareSessions} /> : <div className="banner info" role="status">{sessions.error || '正在读取采集会话…'}</div>)}
          <ViewPanel active={view === 'analysis' || view === 'report'}>
            {activeId ? <Dashboard key={`${activeId}:${revision}`} id={activeId} view={view} disabled={guard.busy} /> : <section className="empty-state panel"><img className="empty-symbol" src="/tj.png" alt="sysmonitor 标志" /><h2>先选择一个分析会话</h2><p>从历史会话打开日志，或前往数据集导入本地日志、连接设备拉取。</p><button className="primary" onClick={() => setView('capture')}>前往数据集</button></section>}
          </ViewPanel>
        </div>
      </main>
    </div>
  )
}