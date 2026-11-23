import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, apiResponse, downloadFilename, errorMessage, formatBytes, formatMemory, formatTime, sessionPath, useResource } from './api'
import type { Session, SessionDeleteResult } from './api'
import { Storage, storageDeleteWarning } from './Storage'
import { SessionMerge } from './SessionMerge'
import { Dashboard } from './Dashboard'
import { ReportDates, reportQuery, useReportDates } from './ReportDates'
import type { ReportStyle } from './Dashboard'
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
  { id: 'capture', label: '数据集', description: 'SysMonitor · Top · QNX 采集 · 本地导入' },
  { id: 'analysis', label: '性能分析', description: 'Android 交互分析 · QNX 静态 SVG 报告' },
  { id: 'compare', label: '双次采集对比', description: 'Android CPU、内存与 IO 差异 · 进程排行' },
  { id: 'report', label: '配置与导出', description: 'Android 分析配置 · 离线 HTML 报告下载' },
  { id: 'advanced', label: '高级功能', description: '采样间隔与采集写入选项' },
  { id: 'storage', label: '存储管理', description: '会话占用 · 批量清理 · 数据库空间回收' },
] as const
type WorkspaceView = typeof workspaceViews[number]['id']

function OperationProgress({ label }: { label: string }) {
  const [started] = useState(() => performance.now())
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => setElapsed(Math.floor((performance.now() - started) / 1000)), 1000)
    return () => window.clearInterval(timer)
  }, [started])
  return <section className="operation-progress" aria-label="操作进度">
    <div className="operation-progress-heading">
      <strong role="status">{label}</strong>
      <span>已耗时 {Math.floor(elapsed / 60)} 分 {elapsed % 60} 秒</span>
    </div>
    <div className="operation-progress-track" role="progressbar" aria-label={label} aria-valuetext="处理中，等待本地服务返回结果"><span /></div>
    <p>正在等待本地服务返回结果，暂无法计算完成百分比。请勿重复操作或关闭页面。</p>
    {elapsed >= 30 && <p>数据量较大时可能需要几分钟，页面仍在等待；最终结果以服务返回为准。</p>}
  </section>
}

function Workspace() {
  const [view, setView] = useState<WorkspaceView>('capture')
  const [reportStyle, setReportStyle] = useState<ReportStyle>('default')
  const currentView = workspaceViews.find((item) => item.id === view)!
  const guard = useEditGuard()
  const [revision, setRevision] = useState(0)
  const appVersion = useResource<{ version: string }>('/api/version')
  const sessions = useResource<Session[]>('/api/sessions', revision)
  const [sessionCache, setSessionCache] = useState<Session[]>()
  // 保留刷新中的列表，同时用删除墓碑过滤旧响应，防止已删会话复活。
  const [removedIds, setRemovedIds] = useState<Set<string>>(new Set())
  if (sessions.data && sessions.data !== sessionCache) setSessionCache(sessions.data)
  const compareSessions = useMemo(() => (sessions.data ?? sessionCache)?.filter((session) => !removedIds.has(session.id)), [sessions.data, sessionCache, removedIds])
  const visibleSessions = compareSessions ?? []
  const [selected, setSelected] = useState<string | null>(null)
  const [pendingSelection, setPendingSelection] = useState<{ id: string; list: Session[] | undefined } | null>(null)
  // A successful creation can precede the refreshed list; do not fall back to an old ID.
  const awaitingList = pendingSelection && (!sessions.data || sessions.data === pendingSelection.list)
  if (pendingSelection && !awaitingList) setPendingSelection(null)
  const activeId = selected && !removedIds.has(selected) && (!sessions.data || (awaitingList && pendingSelection.id === selected) || visibleSessions.some((session) => session.id === selected))
    ? selected : visibleSessions[0]?.id ?? null
  const active = visibleSessions.find((session) => session.id === activeId)
  const isQnx = active?.summary.source_format === 'qnx'
  const reportDates = useReportDates(activeId, revision)
  // 首次列表就绪时固定选择；刷新期间保留当前会话，不通过 effect 追加渲染。
  if (selected !== activeId) setSelected(activeId)
  const [files, setFiles] = useState<File[]>([])
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

  function onDeleted(ids: string[]) {
    const removed = new Set(ids)
    setRemovedIds((previous) => new Set([...previous, ...removed]))
    setSessionCache((previous) => previous?.filter((session) => !removed.has(session.id)))
    setSelected((previous) => previous && !removed.has(previous) ? previous : visibleSessions.find((session) => !removed.has(session.id))?.id ?? null)
    setRevision((value) => value + 1)
  }

  function onImported(id: string) {
    setPendingSelection({ id, list: sessions.data })
    setRemovedIds((previous) => {
      if (!previous.has(id)) return previous
      const next = new Set(previous)
      next.delete(id)
      return next
    })
    setSelected(id)
    setView('analysis')
    setRevision((value) => value + 1)
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
      const result = await api<{ id: string; duplicate: boolean }>('/api/import', { method: 'POST', body: form, signal })
      if (signal.aborted) return
      onImported(result.id)
      setNotice(result.duplicate ? '这些日志已导入，已打开已有会话。' : '导入完成，日志已交由本机服务解析。')
      setFiles([])
      if (fileInput.current) fileInput.current.value = ''
    })
  }

  async function mergeSessions(ids: string[], name: string) {
    if (!guard.confirm('合并成功后将打开新会话并放弃未保存配置，原会话保留。是否继续？')) return false
    let succeeded = false
    await perform('正在合并会话并保留采集分段…', async (signal) => {
      const response = await apiResponse('/api/sessions/merge', {
        method: 'POST', signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_ids: ids, name }),
      }, null)
      const result: { id: string; source_count: number } = await response.json()
      if (signal.aborted) return
      onImported(result.id)
      setNotice(`已将 ${result.source_count} 个会话合并为新会话，原会话保留。各采集段独立，重叠时间点未去重。`)
      succeeded = true
    })
    return succeeded
  }

  function deleteSession() {
    if (!activeId || !guard.confirm() || !window.confirm(`确定删除“${active?.name || '当前会话'}”？\n\n${storageDeleteWarning}`)) return
    void perform('正在删除会话并清理受管日志…', async (signal) => {
      const response = await apiResponse(sessionPath(activeId), { method: 'DELETE', signal }, null)
      const result: SessionDeleteResult = await response.json()
      if (!result.deleted) throw new Error('本地服务未确认删除，请刷新后核实。')
      if (signal.aborted) return
      onDeleted([...result.deleted_ids, ...result.missing_ids])
      setNotice(`会话已删除；实际释放日志 ${formatBytes(result.logs_freed_bytes)}，数据库空间需另行回收。${result.warnings.length ? ` 提示：${result.warnings.join('；')}` : ''}`)
    })
  }

  function exportReport() {
    if (!activeId || !active || !reportDates.canGenerate || !guard.confirm(isQnx ? '下载 QNX 静态 SVG HTML 报告，不会保存或丢弃当前草稿。是否继续下载？' : '有未保存的分析配置或进程组。离线交互报告直接取原始数据，不包含人工配置或进程组；下载不会保存或丢弃当前草稿。是否继续下载？')) return
    void perform('正在生成 HTML 报告…', async (signal) => {
      const response = await apiResponse(`${sessionPath(activeId)}/report${reportQuery(isQnx ? 'default' : reportStyle, reportDates.data?.dates.length ? reportDates.selected : undefined)}`, { signal })
      if (response.headers.get('content-type')?.split(';')[0]?.trim() !== 'text/html') throw new Error('本地服务返回了无效报告格式，请重试。')
      const filename = downloadFilename(response, '.html')
      const blob = await response.blob()
      if (signal.aborted) return
      if (!blob.size) throw new Error('本地服务返回了空报告，请重试。')
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
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
        <div className="brand"><img className="brand-mark" src="/JD_logo.png" alt="京东" width={80} height={40} /><strong>JDPerf</strong></div>
        <div className="workspace-label">本地工作空间 <span className="status-dot" /> 离线分析</div>
        <nav className="workspace-nav" aria-label="工作区功能">{workspaceViews.map((item) => (
          <button key={item.id} disabled={guard.busy} className={view === item.id ? 'active' : ''} aria-current={view === item.id ? 'page' : undefined} onClick={() => setView(item.id)}><strong>{item.label}</strong><small>{item.description}</small></button>
        ))}</nav>
        <div className="history-heading"><h2>历史会话</h2><button className="text-button" disabled={guard.busy || sessions.loading} onClick={() => { if (guard.confirm()) setRevision((value) => value + 1) }}>刷新</button></div>
        {sessions.loading && <p className="sidebar-hint" role="status">正在读取本地历史…</p>}
        {sessions.error && <p className="sidebar-hint error" role="alert">{sessions.error}</p>}
        {compareSessions?.length === 0 && <p className="sidebar-hint">还没有会话，导入日志后将在此保留。</p>}
        <nav className="session-list" aria-label="历史分析会话">{visibleSessions.map((session) => (
          <button key={session.id} className={`session-item ${activeId === session.id ? 'active' : ''}`} disabled={guard.busy} aria-current={activeId === session.id ? 'page' : undefined} onClick={() => { if (activeId === session.id || !guard.confirm()) return; setSelected(session.id); setError(''); setNotice('') }}>
            <strong>{session.name}</strong><small>{createdLabel(session.created)}</small><span>{session.summary.files.length} 个文件 · {session.summary.cycles} 个{session.summary.source_format === 'qnx' ? '时间点 · QNX' : '周期'}</span>
          </button>
        ))}</nav>
        <div className="sidebar-footer">日志留在本机<br />离线导入 / 趋势分析 / HTML 报告</div>
      </aside>
      <main>
        <header className="topbar"><span>性能工作台 <b>/</b> {currentView.label}</span><div className="app-info"><span className="app-version" title={appVersion.error ? `版本读取失败：${appVersion.error}` : '当前应用版本'}>{appVersion.data ? `JDPerf v${appVersion.data.version}` : appVersion.loading ? '版本读取中…' : '版本暂不可用'}</span><span className="badge">本地服务 · 127.0.0.1</span></div></header>
        <div className="main-content">
          <div className="page-heading"><div><p className="eyebrow">{view === 'analysis' || view === 'report' ? (active ? (isQnx ? 'QNX' : 'ANDROID') : 'PERFORMANCE') : 'PERFORMANCE'} / {currentView.label}</p><h1>{currentView.label}</h1><p>{isQnx && (view === 'analysis' || view === 'report') ? 'QNX 静态 SVG 分析 · 离线 HTML 报告下载' : currentView.description}{active && (view === 'analysis' || view === 'report') ? ` · 当前会话：${active.name}` : ''}</p></div>
            <div className="actions">{(view === 'analysis' || view === 'report') && <>
              {active && !isQnx && <label>报告样式 <select value={reportStyle} disabled={guard.busy} onChange={(event) => {
                const next = event.target.value as ReportStyle
                if (next !== reportStyle && window.confirm('切换样式将清空报告内的搜索、曲线选择与合并结果，不影响原始数据或未保存配置。是否继续？')) setReportStyle(next)
              }}><option value="default">原版完整报告</option><option value="compact">简洁组合图</option></select></label>}
              <button className="primary" disabled={!active || guard.busy || !reportDates.canGenerate} onClick={exportReport}>{isQnx ? '导出 QNX 静态 HTML' : '导出当前样式 HTML'}</button>
              <button className="danger" disabled={!activeId || guard.busy} onClick={deleteSession}>删除会话</button>
            </>}</div>
          </div>
          {activeId && (view === 'analysis' || view === 'report') && <ReportDates scope={reportDates} disabled={guard.busy} />}
          <Devices view={view} onAdvanced={() => setView('advanced')} disabled={guard.busy} onImported={onImported} />
          <ViewPanel active={view === 'capture'}>
            <details className="panel local-import">
              <summary>导入本地日志 <span>已有日志文件时展开</span></summary>
            <form className="import-form capture-import" onSubmit={importFiles}>
              <h2>导入本地日志</h2><p>支持 Android 性能日志及 QNX 归一化日志 perf_qnx.log，可离线导入；QNX 原始 tar 归档不能直接作为分析日志上传。每次最多 5 个文件，单文件最大 1 GB，仅发送给本地分析服务。大文件解析需要较长时间及足够磁盘空间。</p>
              <label className="file-picker">选择日志文件<input ref={fileInput} type="file" multiple disabled={guard.busy} onChange={(event) => setFiles(Array.from(event.target.files || []))} /></label>
              {files.length > 0 && <div className="file-selection"><strong>已选择 {files.length} 个文件</strong><ul>{files.map((file, index) => <li key={index} title={file.name}>{file.name} · {formatMemory(file.size / 1024)}</li>)}</ul></div>}
              <button className="primary" type="submit" disabled={!files.length || guard.busy}>{busy.startsWith('正在本地') ? '正在解析…' : '导入并分析'}</button>
            </form>
            </details>
            <SessionMerge sessions={visibleSessions} disabled={guard.busy || sessions.loading} onMerge={mergeSessions} />
          </ViewPanel>
          {guard.dirty && <div className="banner warning" role="status">有未保存的进程组或人工分析配置，请在对应表单保存以保留用于分析。离线报告直接取原始数据，不包含人工配置或进程组；下载不会保存或丢弃草稿。</div>}
          {busy && <OperationProgress label={busy} />}
          {error && <div className="banner error" role="alert">{error}</div>}
          {notice && <div className="banner success" role="status">{notice}</div>}
          {view === 'storage' && <Storage sessions={visibleSessions} perform={perform} onDeleted={onDeleted} onNotice={setNotice} />}
          {view === 'compare' && (compareSessions ? <Compare sessions={compareSessions} /> : <div className="banner info" role="status">{sessions.error || '正在读取采集会话…'}</div>)}
          <ViewPanel active={view === 'analysis' || view === 'report'}>
            {activeId ? <Dashboard key={`${activeId}:${revision}`} id={activeId} view={view} disabled={guard.busy} reportStyle={reportStyle} reportDates={reportDates} /> : <section className="empty-state panel"><img className="empty-symbol" src="/JD_logo.png" alt="JDPerf 标志" width={120} height={60} /><h2>先选择一个分析会话</h2><p>从历史会话打开日志，或前往数据集导入本地日志、连接设备拉取。</p><button className="primary" onClick={() => setView('capture')}>前往数据集</button></section>}
          </ViewPanel>
        </div>
      </main>
    </div>
  )
}