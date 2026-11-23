import { useState } from 'react'
import type { FormEvent } from 'react'
import { formatTime } from './api'
import type { Session } from './api'

interface Props {
  sessions: Session[]
  disabled: boolean
  onMerge: (ids: string[], name: string) => Promise<boolean>
}

export function SessionMerge({ sessions, disabled, onMerge }: Props) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [date, setDate] = useState('')
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const locked = disabled || submitting
  const start = date ? new Date(`${date}T00:00:00`).getTime() : null
  const end = date ? new Date(`${date}T00:00:00`) : null
  if (end) end.setDate(end.getDate() + 1)
  const visible = sessions.filter((session) => {
    const summary = session.summary
    return (!query.trim() || `${session.name} ${summary.device_name ?? ''}`.toLowerCase().includes(query.trim().toLowerCase()))
      && (start === null || (summary.first_ts != null && summary.last_ts != null
        && summary.first_ts < end!.getTime() && summary.last_ts >= start))
  }).sort((a, b) => (a.summary.first_ts ?? Infinity) - (b.summary.first_ts ?? Infinity))
  // Only visible, still-existing sessions can be submitted; stale IDs are never sent.
  const chosen = visible.filter((session) => selected.has(session.id))
  const formats = new Set(chosen.map((session) => session.summary.source_format ?? 'sysmonitor'))
  const devices = new Set(chosen.map((session) => session.summary.device_name).filter((value) => value && value !== '未知设备'))
  const warning = formats.size > 1 ? '请选择相同日志格式的会话。' : devices.size > 1 ? '所选会话来自不同设备，请重新选择。' : ''

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (locked || chosen.length < 2 || chosen.length > 1000 || warning) return
    setSubmitting(true)
    try {
      if (await onMerge(chosen.map((session) => session.id), name.trim())) {
        setSelected(new Set())
        setName('')
      }
    } finally { setSubmitting(false) }
  }

  return <details className="panel local-import session-merge">
    <summary>合并已有会话 <span>将当天多次采集合并成一个新会话，原会话保留</span></summary>
    <form onSubmit={submit}>
      <p className="merge-note">按采集时间排序，保留各采集分段，中断处不连线、不补值。时间重叠的实测记录均保留并参与统计；请避免选择重复采集的数据。不会继承原会话的分析配置或进程组。</p>
      <div className="merge-filters">
        <label>采集日期<input aria-label="采集日期" type="date" value={date} disabled={locked} onChange={(event) => { setDate(event.target.value); setSelected(new Set()) }} /></label>
        <label>名称或设备<input aria-label="名称或设备" type="search" value={query} disabled={locked} placeholder="筛选已有会话" onChange={(event) => { setQuery(event.target.value); setSelected(new Set()) }} /></label>
        <button type="button" disabled={locked || (!date && !query)} onClick={() => { setDate(''); setQuery(''); setSelected(new Set()) }}>清除筛选</button>
      </div>
      <p className="merge-note">日期按本机时区匹配采集时间范围；筛选不裁剪会话内容，合并会包含所选会话的全部日期数据。更改筛选会清空勾选。</p>
      <div className="merge-actions">
        <button type="button" disabled={locked || !visible.length} onClick={() => setSelected(new Set(visible.slice(0, 1000).map((session) => session.id)))}>全选筛选结果</button>
        <button type="button" disabled={locked || !chosen.length} onClick={() => setSelected(new Set())}>清空选择</button>
        <span role="status">已选 {chosen.length} / {visible.length} 个会话（每次 2–1000 个）</span>
      </div>
      <div className="merge-list" role="group" aria-label="选择待合并会话">
        {visible.map((session) => <label className="merge-row" key={session.id}>
          <input type="checkbox" aria-label={`选择 ${session.name}`} checked={selected.has(session.id)} disabled={locked || (!selected.has(session.id) && chosen.length >= 1000)} onChange={(event) => {
            const checked = event.target.checked
            if (locked || (checked && chosen.length >= 1000)) return
            setSelected((previous) => { const next = new Set(previous); if (checked) next.add(session.id); else next.delete(session.id); return next })
          }} />
          <span><strong>{session.name}</strong><small>{session.summary.device_name || '未知设备'} · {session.summary.source_format || 'sysmonitor'} · {session.summary.cycles} 个周期{session.summary.merge_sources ? ' · 已合并会话' : ''}</small><small>{formatTime(session.summary.first_ts)} 至 {formatTime(session.summary.last_ts)}</small></span>
        </label>)}
        {!visible.length && <p className="merge-note">没有符合条件的会话，请调整筛选或先导入日志。</p>}
      </div>
      {warning && <p className="banner warning" role="alert">{warning}</p>}
      <div className="merge-filters">
        <label>新会话名称（可选）<input aria-label="新会话名称" value={name} maxLength={120} disabled={locked} placeholder="留空将自动命名" onChange={(event) => setName(event.target.value)} /></label>
        <button className="primary" type="submit" disabled={locked || chosen.length < 2 || !!warning}>{submitting ? '正在合并…' : '合并并分析'}</button>
      </div>
    </form>
  </details>
}