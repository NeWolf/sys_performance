import { useState } from 'react'
import { formatTime, sessionPath, useResource, useAnalysisStream } from './api'
import { AnalysisProgress } from './Progress'
import type { Overview, Session } from './api'
import { Results } from './CompareResults'
import type { Comparison } from './CompareResults'
import { downloadComparisonReport } from './compareReport'

function SessionChoice({ label, labelValue, onLabelChange, sessions, id, segment, onChange, onSegment }: { label: string; labelValue: string; onLabelChange: (value: string) => void; sessions: Session[]; id: string; segment: string; onChange: (id: string) => void; onSegment: (value: string) => void }) {
  const detail = useResource<Overview>(id ? sessionPath(id) : null)
  return <div className="compare-choice">
    <label>显示标签<input value={labelValue} onChange={(e) => onLabelChange(e.target.value)} placeholder={label} maxLength={40} /></label>
    <label>{label}<select value={id} onChange={(e) => onChange(e.target.value)}><option value="">选择采集会话</option>{sessions.map((s) => <option key={s.id} value={s.id}>{s.name} · {formatTime(typeof s.created === 'string' ? Date.parse(s.created) : s.created)} · {s.id.slice(0, 8)}</option>)}</select></label>
    <label>时段<select value={segment} onChange={(e) => onSegment(e.target.value)} disabled={!detail.data}><option value="">全部时段</option>{detail.data?.segments.map((s) => <option key={s.segment} value={s.segment}>时段 {s.segment} · {formatTime(s.start)} — {formatTime(s.end)}</option>)}</select></label>
    {detail.loading && <small>正在读取时段…</small>}{detail.error && <small role="alert">{detail.error}</small>}
  </div>
}

export function Compare({ sessions }: { sessions: Session[] }) {
  const [baseline, setBaseline] = useState(sessions[1]?.id ?? sessions[0]?.id ?? '')
  const [target, setTarget] = useState(sessions.length > 1 ? sessions[0].id : '')
  const [baselineSegment, setBaselineSegment] = useState('')
  const [targetSegment, setTargetSegment] = useState('')
  const [baselineName, setBaselineName] = useState('')
  const [targetName, setTargetName] = useState('')
  const baselineLabel = baselineName.trim() || '基准'
  const targetLabel = targetName.trim() || '对比'
  const [requested, setRequested] = useState('')
  const [run, setRun] = useState(0)
  const params = new URLSearchParams({ baseline, target })
  if (baselineSegment !== '') params.set('baseline_segment', baselineSegment)
  if (targetSegment !== '') params.set('target_segment', targetSegment)
  const path = `/api/compare/stream?${params}`
  const valid = baseline !== target && sessions.some((s) => s.id === baseline) && sessions.some((s) => s.id === target)
  const result = useAnalysisStream<Comparison>(valid && requested === path ? path : null, run)
  return <div className="compare-view"><section className="panel compare-section"><h2>选择两次采集</h2><p>建议选择相同场景和采样设置；两侧可分别选择完整时段。可设置显示标签（最多 40 字，留空使用默认名称），修改标签无需重新计算。</p>{sessions.length < 2 && <p role="status">请先导入或采集至少两个不同会话。</p>}<div className="compare-columns">
    <SessionChoice label={baselineLabel} labelValue={baselineName} onLabelChange={setBaselineName} sessions={sessions} id={baseline} segment={baselineSegment} onChange={(id) => { setBaseline(id); setBaselineSegment(''); setRequested('') }} onSegment={(s) => { setBaselineSegment(s); setRequested('') }} />
    <SessionChoice label={targetLabel} labelValue={targetName} onLabelChange={setTargetName} sessions={sessions} id={target} segment={targetSegment} onChange={(id) => { setTarget(id); setTargetSegment(''); setRequested('') }} onSegment={(s) => { setTargetSegment(s); setRequested('') }} />
  </div><div className="actions"><button onClick={() => { setBaseline(target); setTarget(baseline); setBaselineName(targetName); setTargetName(baselineName); setBaselineSegment(targetSegment); setTargetSegment(baselineSegment); setRequested('') }}>交换两侧</button><button className="primary" disabled={!valid || result.loading} onClick={() => { setRequested(path); setRun(run + 1) }}>{result.loading ? '正在计算…' : result.error ? '重新对比' : '开始对比'}</button>{result.loading && <button onClick={() => setRequested('')}>取消对比</button>}</div>{baseline && baseline === target && <p role="alert">请选择两次不同的采集。</p>}</section>
    {!result.data && <div className="actions"><button className="primary" disabled title="请先完成两次采集对比">导出对比报告（HTML）</button><span className="muted">{result.loading ? '正在计算，完成后即可导出报告。' : '请先点击“开始对比”，完成后即可导出报告。'}</span></div>}
    {result.loading && <AnalysisProgress baselineLabel={baselineLabel} targetLabel={targetLabel} progress={result.progress} elapsed={result.elapsed} />}
    {result.error && <div className="banner error" role="alert">{result.error}</div>}
    {result.data && <Results baselineLabel={baselineLabel} targetLabel={targetLabel} key={`${requested}:${run}`} data={result.data} onExport={(view) => {
      const date = new Date()
      return downloadComparisonReport({ data: result.data!, baselineLabel, targetLabel, view, generatedAt: date.toLocaleString('zh-CN') }, date)
    }} />}
  </div>
}