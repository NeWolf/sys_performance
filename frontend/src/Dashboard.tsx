import { useEffect, useState } from 'react'
import { apiResponse, errorMessage, formatNumber, formatTime, sessionPath, useResource } from './api'
import type { Overview } from './api'
import { Groups } from './Groups'
import { ReportConfig } from './ReportConfig'
import { ViewPanel } from './ViewPanel'

type DashboardProps = { id: string; disabled: boolean; view: string }

export function Dashboard({ id, disabled, view }: DashboardProps) {
  const [revision, setRevision] = useState(0)
  const { data, error, loading } = useResource<Overview>(sessionPath(id), revision)
  if (loading) return <div className="panel loading-state" role="status">正在读取会话统计…</div>
  if (error) return <div className="banner error" role="alert">{error} <button disabled={disabled} onClick={() => setRevision((value) => value + 1)}>重试</button></div>
  if (!data) return null
  return <Analysis data={data} disabled={disabled} view={view} />
}

function Analysis({ data, disabled, view }: { data: Overview; disabled: boolean; view: string }) {
  const [reportTab, setReportTab] = useState('groups')
  const [reportRevision, setReportRevision] = useState(0)
  const [segment, setSegment] = useState(data.segments[0]?.segment ?? 0)
  return <>
    <ViewPanel active={view === 'analysis'}>
      <InteractiveReport id={data.id} name={data.name} disabled={disabled} />
      <details className="panel diagnostics">
        <summary>日志质量与来源</summary>
        <p className="muted">{formatNumber(data.summary.cycles, 0)} 个周期 · {data.summary.segments} 个时间段 · {formatTime(data.summary.first_ts)} → {formatTime(data.summary.last_ts)}</p>
        <div className="quality-row"><span>解析异常 <b>{data.summary.errors}</b></span><span>未知行 <b>{data.summary.unknown}</b></span><span>孤立周期 <b>{data.summary.orphan_cycles}</b></span></div>
        <details><summary>来源文件（{data.summary.files.length}）</summary><ul>{data.summary.files.map((file, index) => <li key={index}>{file}</li>)}</ul></details>
        <details><summary>解析异常明细（{data.summary.issues.length}）</summary><ul className="log-list">{data.summary.issues.map((item, index) => <li key={index}><strong>{item.source} : {item.line}</strong><span>{item.message}</span></li>)}</ul></details>
        <details><summary>日志事件（{data.summary.events.length}）</summary><ul className="log-list">{data.summary.events.map((item, index) => <li key={index}><strong>{item.source} : {item.line} · {formatTime(item.ts)}</strong><span>{item.message}</span></li>)}</ul></details>
      </details>
    </ViewPanel>
    <ViewPanel active={view === 'report'}>
      <p className="banner info">应用内性能分析与离线导出使用同一份报告。下方人工配置与进程组仅用于独立分析，不写入报告；下载不会保存或丢弃草稿。</p>
      <nav className="section-tabs" aria-label="分析配置分区">
        {[{ id: 'groups', label: '进程组分析设置' }, { id: 'content', label: '人工分析配置' }].map((item) => (
          <button key={item.id} aria-pressed={reportTab === item.id} className={reportTab === item.id ? 'active' : ''} onClick={() => setReportTab(item.id)}>{item.label}</button>
        ))}
      </nav>
      <ViewPanel active={view === 'report' && reportTab === 'groups'}>
        <Groups id={data.id} segment={segment} onSegment={setSegment} disabled={disabled} settingsRevision={reportRevision} />
      </ViewPanel>
      <ViewPanel active={view === 'report' && reportTab === 'content'}>
        <ReportConfig id={data.id} disabled={disabled} onSaved={() => setReportRevision((value) => value + 1)} />
      </ViewPanel>
    </ViewPanel>
  </>
}

function InteractiveReport({ id, name, disabled }: { id: string; name: string; disabled: boolean }) {
  const [revision, setRevision] = useState(0)
  const [expanded, setExpanded] = useState(false)
  const [result, setResult] = useState<{ html: string; error: string; loading: boolean }>({ html: '', error: '', loading: true })
  useEffect(() => {
    const controller = new AbortController()
    async function load() {
      try {
        const response = await apiResponse(`${sessionPath(id)}/report`, { signal: controller.signal })
        const html = await response.text()
        if (!html.trim()) throw new Error('本地服务返回了空报告，请重试。')
        if (!controller.signal.aborted) setResult({ html, error: '', loading: false })
      } catch (failure) {
        if (!controller.signal.aborted) setResult({ html: '', error: errorMessage(failure), loading: false })
      }
    }
    void load()
    return () => controller.abort()
  }, [id, revision])
  useEffect(() => {
    if (!expanded) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false) }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [expanded])
  function reload() {
    if (!window.confirm('重新加载将清空报告内的搜索、曲线选择与合并结果，是否继续？')) return
    setResult({ html: '', error: '', loading: true })
    setRevision((value) => value + 1)
  }
  return <section className={`analysis-report${expanded ? ' analysis-report-expanded' : ''}`} aria-label="交互性能分析">
    <div className="analysis-report-toolbar">
      <div><strong>交互性能分析</strong><p>与导出报告一致 · 按采集段查看 · 搜索、排序、趋势叠加与精确合并</p></div>
      <div className="actions">
        <button disabled={disabled || result.loading} onClick={reload}>重新加载</button>
        <button aria-pressed={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? '退出大屏' : '大屏查看'}</button>
      </div>
    </div>
    {result.loading && <div className="panel loading-state" role="status">正在生成完整分析，大日志可能需要较长时间…</div>}
    {result.error && <div className="banner error" role="alert">{result.error}<button disabled={disabled} onClick={() => { setResult({ html: '', error: '', loading: true }); setRevision((value) => value + 1) }}>重试</button></div>}
    {/* 保留报告自身 CSP；不授予同源、弹窗、下载或顶层导航权限。 */}
    {result.html && <iframe className="analysis-report-frame" title={`${name} · 性能分析`} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={result.html} />}
    <p className="analysis-report-note">图表缩放不改变全量统计；未采集保持缺失。大屏模式可用顶部按钮退出。</p>
  </section>
}

// 报告文档在独立沙箱内运行，不向应用 DOM 注入日志或报告脚本。