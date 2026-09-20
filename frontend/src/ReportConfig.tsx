import { useState } from 'react'
import type { FormEvent } from 'react'
import { api, sessionPath, useResource } from './api'
import type { ReportSettings } from './api'
import { useDirty, useEditGuard, useMutation } from './Editing'

const defaults: ReportSettings = { cpu_platform: '', kdmips_per_core: null, hardware: '', software: '', tester: '', test_notes: '', worst_scenarios: '', analysis: '', criteria: '', conclusion: '待评估', conclusion_notes: '' }
const fields: { key: Exclude<keyof ReportSettings, 'conclusion' | 'kdmips_per_core'>; label: string; max: number }[] = [
  { key: 'cpu_platform', label: 'CPU 平台与系数适用范围', max: 120 },
  { key: 'hardware', label: '硬件环境', max: 2000 },
  { key: 'software', label: '软件环境', max: 2000 },
  { key: 'tester', label: '测试人员', max: 200 },
  { key: 'test_notes', label: '测试说明', max: 4000 },
  { key: 'worst_scenarios', label: '全场景最坏情况说明', max: 8000 },
  { key: 'analysis', label: '资源分析说明', max: 8000 },
  { key: 'criteria', label: '准入标准', max: 8000 },
  { key: 'conclusion_notes', label: '准入结论说明', max: 8000 },
]

export function ReportConfig({ id, disabled, onSaved }: { id: string; disabled: boolean; onSaved: () => void }) {
  const [revision, setRevision] = useState(0)
  const path = `${sessionPath(id)}/report-settings`
  const { data, loading, error } = useResource<Partial<ReportSettings>>(path, revision)
  return <>
    <div className="section-heading"><h2>人工分析配置（原报告配置）</h2><span>保留用于分析 · 不写入离线交互报告</span></div>
    <section className="panel config-panel">
      <p>离线交互 HTML 报告直接取原始数据，展示系统资源、全部同段同名进程和 Excel 49项核对，包含 CPU、RSS、物理与逻辑 I/O。支持离线搜索排序、曲线叠加和同段精确合并；未采集标记缺失，I/O 不作判定。脚本关闭时仍可查看静态表格和进程图。</p>
      <p className="banner info">原有配置与进程组仍可保存用于分析。下载不再包含六章节、人工环境与结论、进程组或配置（无论是否已保存）；草稿不会自动保存。</p>
      {loading && <p role="status">正在读取人工分析配置…</p>}
      {error && <p className="banner error" role="alert">{error} <button disabled={disabled} onClick={() => setRevision((v) => v + 1)}>重试读取</button></p>}
      {data && <ReportForm key={`${id}:${revision}`} path={path} initial={{ ...defaults, ...data }} disabled={disabled} onSaved={onSaved} reload={() => setRevision((v) => v + 1)} />}
    </section>
  </>
}

function ReportForm({ path, initial, disabled, reload, onSaved }: { path: string; initial: ReportSettings; disabled: boolean; reload: ()=> void; onSaved: () => void }) {
  const [draft, setDraft] = useState(initial)
  const [baseline, setBaseline] = useState(initial)
  const [saved, setSaved] = useState(false)
  const mutation = useMutation()
  const guard = useEditGuard()
  const locked = disabled || guard.busy
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline)
  useDirty(dirty)
  function confirmDiscard() { return !locked && (!dirty || window.confirm('人工分析配置有未保存改动，是否放弃？')) }
  function save(event: FormEvent) {
    event.preventDefault()
    if (locked) return
    setSaved(false)
    mutation.setError('')
    if (draft.kdmips_per_core !== null && (!Number.isFinite(draft.kdmips_per_core) || draft.kdmips_per_core <= 0 || !draft.cpu_platform.trim())) {
      mutation.setError('配置系数时须填写 CPU 平台，系数必须为正有限数。'); return
    }
    void mutation.run(async (signal) => {
      const result = await api<Partial<ReportSettings>>(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft), signal })
      if (!signal.aborted) { const value = { ...defaults, ...result }; setDraft(value); setBaseline(value); setSaved(true); onSaved() }
    })
  }
  return <form onSubmit={save}>
    <fieldset disabled={locked} className="config-fields"><div className="form-grid">
      {fields.map(({ key, label, max }) => <label key={key} className={max >= 4000 ? 'full-width' : ''}>{label}{!draft[key].trim() && <small className="muted">待填写</small>}
        <textarea rows={max >= 4000 ? 4 : 2} maxLength={max} value={draft[key]} placeholder="待填写" onChange={(event) => { setDraft({ ...draft, [key]: event.target.value }); setSaved(false) }} />
        <small className="muted">{draft[key].length} / {max}</small>
      </label>)}
      <label>单核 KDMIPS 系数<input type="number" step="any" value={draft.kdmips_per_core ?? ''} placeholder="未配置" onChange={(event) => { setDraft({ ...draft, kdmips_per_core: event.target.value === '' ? null : Number(event.target.value) }); setSaved(false) }} /><small className="muted">K = 单核 CPU 百分比 ÷ 100 × 系数；不按核心数再除。无默认值，未配置时 K 显示 —。仅对填写的平台有效。</small></label>
      <label>准入结论<select value={draft.conclusion} onChange={(event) => { setDraft({ ...draft, conclusion: event.target.value as ReportSettings['conclusion'] }); setSaved(false) }}>
        {(['待评估', '准入', '有条件准入', '不准入'] as const).map((value) => <option key={value}>{value}</option>)}
      </select></label>
    </div><div className="actions config-actions">
      <button type="submit" className="primary" disabled={!dirty}>{mutation.busy ? '保存中…' : '保存人工分析配置'}</button>
      <button type="button" disabled={!dirty} onClick={() => { if (confirmDiscard()) { setDraft(baseline); setSaved(false); mutation.setError('') } }}>撤销改动</button>
      <button type="button" onClick={() => { if (confirmDiscard()) reload() }}>重新读取</button>
      <span>{dirty ? '有未保存改动' : '无未保存改动'}</span>
    </div></fieldset>
    {mutation.error && <p className="banner error" role="alert">{mutation.error} 草稿已保留，请再次点击保存重试。</p>}
    {saved && !dirty && <p className="banner success" role="status">人工分析配置已保存，保留用于分析；离线交互 HTML 报告直接取原始数据，不包含这些配置。</p>}
  </form>
}