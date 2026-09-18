import { useState } from 'react'
import type { FormEvent } from 'react'
import { api, groupPath, useResource } from './api'
import type { GroupConfig, GroupMember, ProcessGroup, ProcessRow } from './api'
import { useDirty, useEditGuard, useMutation } from './Editing'
import { Processes } from './Processes'
import { GroupResults } from './GroupResults'

const emptyGroup = (segment: number): GroupConfig => ({ name: '', segment, members: [], scene: '未标注', start: null, end: null, description: '' })
const configOf = (group: GroupConfig): GroupConfig => ({ name: group.name, segment: group.segment, members: group.members, scene: group.scene, start: group.start, end: group.end, description: group.description })

export function Groups({ id, segment, onSegment, disabled, settingsRevision }: { id: string; segment: number; onSegment: (value: number) => void; disabled: boolean; settingsRevision: number }) {
  const [revision, setRevision] = useState(0)
  const groups = useResource<ProcessGroup[]>(groupPath(id), revision)
  const [draft, setDraft] = useState(() => emptyGroup(segment))
  const [baseline, setBaseline] = useState(() => JSON.stringify(emptyGroup(segment)))
  const [editing, setEditing] = useState('')
  const [analysis, setAnalysis] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [notice, setNotice] = useState('')
  const [failedLoad, setFailedLoad] = useState('')
  const mutation = useMutation()
  const guard = useEditGuard()
  const locked = disabled || guard.busy
  const dirty = JSON.stringify(draft) !== baseline || start !== String(draft.start ?? '') || end !== String(draft.end ?? '')
  useDirty(dirty)
  function reset(config: GroupConfig, gid = '') {
    const clean = configOf(config)
    setDraft(clean); setBaseline(JSON.stringify(clean)); setEditing(gid)
    setStart(String(clean.start ?? '')); setEnd(String(clean.end ?? ''))
    mutation.setError(''); setFailedLoad('')
  }
  function mayDiscard() { return !locked && (!dirty || window.confirm('当前进程组有未保存改动，是否放弃？')) }
  function load(gid: string) {
    if (!mayDiscard()) return
    setNotice(''); setFailedLoad(gid)
    void mutation.run(async (signal) => {
      const value = await api<ProcessGroup>(groupPath(id, gid), { signal })
      if (!signal.aborted) { reset(value, value.id); setAnalysis(value.id) }
    })
  }
  function toggle(row: ProcessRow) {
    if (locked) return
    const found = draft.segment === row.segment && draft.members.some((item) => item.pid === row.pid && item.name === row.name)
    if (!found && ((draft.members.length > 0 && draft.segment !== row.segment) || draft.members.length >= 50)) return
    const member: GroupMember = { pid: row.pid, name: row.name, process_type: '', service_category: '未分类', related_service: '', purpose: '', foreground: '待填写', background: '待填写' }
    setDraft({ ...draft, segment: row.segment, members: found ? draft.members.filter((item) => item.pid !== row.pid || item.name !== row.name) : [...draft.members, member] })
    setNotice('')
  }
  function updateMember(index: number, patch: Partial<GroupMember>) {
    setDraft({ ...draft, members: draft.members.map((member, i) => i === index ? { ...member, ...patch } : member) })
  }
  function save(event: FormEvent) {
    event.preventDefault()
    if (locked) return
    mutation.setError(''); setNotice(''); setFailedLoad('')
    const parse = (text: string) => text.trim() === '' ? null : /^-?\d+$/.test(text.trim()) && Number.isSafeInteger(Number(text)) ? Number(text) : NaN
    const from = parse(start), to = parse(end)
    if ((from !== null && !Number.isFinite(from)) || (to !== null && !Number.isFinite(to)) || (from !== null && to !== null && from > to)) {
      mutation.setError('时间必须为可精确表示的毫秒整数，且开始不能晚于结束；留空表示不限。'); return
    }
    if (!draft.name.trim() || !draft.members.length) { mutation.setError('请填写组名并勾选 1–50 个同段成员。'); return }
    const payload: GroupConfig = { ...configOf(draft), name: draft.name.trim(), start: from, end: to }
    void mutation.run(async (signal) => {
      const value = await api<ProcessGroup>(groupPath(id, editing), { method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), signal })
      if (!signal.aborted) { reset(value, value.id); setAnalysis(value.id); setRevision((v) => v + 1); setNotice('进程组已保存，分析及下载将使用此配置。') }
    })
  }
  function remove() {
    if (locked || !editing || !window.confirm(`确定删除进程组“${draft.name}”？${dirty ? '未保存的改动也会丢失。' : ''}此操作不能撤销。`)) return
    setNotice(''); setFailedLoad('')
    void mutation.run(async (signal) => {
      const result = await api<{ deleted: boolean }>(groupPath(id, editing), { method: 'DELETE', signal })
      if (!result.deleted) throw new Error('服务未确认删除，请重试或刷新列表核实。')
      if (!signal.aborted) { if (analysis === editing) setAnalysis(''); reset(emptyGroup(segment)); setRevision((v) => v + 1); setNotice('进程组已删除。') }
    })
  }
  return <>
    <div className="section-heading"><h2>准入进程组</h2><span>单段精确 PID + 名称 · 每组最多 50 个成员 · 每会话最多 20 组</span></div>
    <section className="panel config-panel">
      <div className="config-toolbar"><label>编辑已保存组 <select value={editing} disabled={locked || groups.loading} onChange={(event) => { if (event.target.value) load(event.target.value); else if (mayDiscard()) { reset(emptyGroup(segment)); setNotice('') } }}>
        <option value="">新建进程组</option>{groups.data?.map((group) => <option key={group.id} value={group.id}>{group.name} · 段 {group.segment}</option>)}
      </select></label><button disabled={locked || groups.loading} onClick={() => setRevision((v) => v + 1)}>刷新组列表</button></div>
      {groups.loading && <p role="status">正在读取进程组…</p>}
      {groups.error && <p className="error" role="alert">{groups.error} <button disabled={locked} onClick={() => setRevision((v) => v + 1)}>重试列表</button></p>}
      <form onSubmit={save}><fieldset disabled={locked} className="config-fields">
        <div className="form-grid">
          <label>组名<input required maxLength={120} value={draft.name} onChange={(event) => { setDraft({ ...draft, name: event.target.value }); setNotice('') }} placeholder="待填写" /></label>
          <label>场景<select value={draft.scene} onChange={(event) => setDraft({ ...draft, scene: event.target.value as GroupConfig['scene'] })}>{['未标注', '前台', '后台'].map((value) => <option key={value}>{value}</option>)}</select></label>
          <label>开始时间（毫秒，含）<input inputMode="numeric" value={start} onChange={(event) => setStart(event.target.value)} placeholder="留空：不限" /></label>
          <label>结束时间（毫秒，含）<input inputMode="numeric" value={end} onChange={(event) => setEnd(event.target.value)} placeholder="留空：不限" /></label>
          <label className="full-width">组描述<textarea maxLength={4000} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="待填写" /></label>
        </div>
        <p>已选 {draft.members.length} / 50 个成员 · 时间段 {draft.segment} · 下方排行可跨页增删成员，切换排序不清空选择。</p>
        <p className="muted">不会自动合并 WebView。更换时间段需先移除全部成员；PID 超出网页安全整数范围时禁止选择，避免错误匹配。</p>
        <div className="table-scroll"><table className="member-table"><thead><tr><th>成员名称 / PID</th><th>进程类型</th><th>用途</th><th>服务分类 / 关联服务</th><th>前台需求</th><th>后台需求</th><th>操作</th></tr></thead><tbody>{draft.members.map((member, index) => <tr key={JSON.stringify([member.pid, member.name])}>
          <td className="wrap-text">{member.name}<small>PID {member.pid}</small></td>
          <td><input aria-label={`${member.name} 进程类型`} maxLength={120} value={member.process_type} placeholder="待填写" onChange={(event) => updateMember(index, { process_type: event.target.value })} /></td>
          <td><textarea aria-label={`${member.name} 用途`} maxLength={2000} value={member.purpose} placeholder="待填写" onChange={(event) => updateMember(index, { purpose: event.target.value })} /></td>
          <td><select aria-label={`${member.name} 服务分类`} value={member.service_category} onChange={(event) => updateMember(index, { service_category: event.target.value as GroupMember['service_category'] })}>{['未分类', '应用服务', '系统服务'].map((value) => <option key={value}>{value}</option>)}</select><textarea aria-label={`${member.name} 关联服务`} maxLength={2000} value={member.related_service} placeholder="关联服务（待填写）" onChange={(event) => updateMember(index, { related_service: event.target.value })} /></td>
          {(['foreground', 'background'] as const).map((field) => <td key={field}><select aria-label={`${member.name} ${field === 'foreground' ? '前台' : '后台'}`} value={member[field]} onChange={(event) => updateMember(index, { [field]: event.target.value })}>{['待填写', 'Y', 'N'].map((value) => <option key={value}>{value}</option>)}</select></td>)}
          <td><button type="button" onClick={() => setDraft({ ...draft, members: draft.members.filter((_, i) => i !== index) })}>移除</button></td>
        </tr>)}</tbody></table></div>
        <div className="actions config-actions"><button className="primary" type="submit" disabled={!dirty || !draft.members.length || (!editing && (groups.data?.length ?? 0) >= 20)}>{mutation.busy ? '处理中…' : editing ? '保存组修改' : '保存新组'}</button>
          <button type="button" onClick={() => { if (mayDiscard()) { reset(JSON.parse(baseline) as GroupConfig, editing); setNotice('') } }} disabled={!dirty}>撤销改动</button>
          <button type="button" className="danger" disabled={!editing} onClick={remove}>删除组</button><span>{dirty ? '有未保存改动' : '无未保存改动'}</span></div>
      </fieldset></form>
      {mutation.error && <p className="banner error" role="alert">{mutation.error} {failedLoad ? <button disabled={locked} onClick={() => load(failedLoad)}>重试读取组</button> : '草稿已保留，可再次保存或删除重试。'}</p>}
      {notice && !dirty && <p className="banner success" role="status">{notice}</p>}
    </section>
    <Processes id={id} segment={segment} onSegment={onSegment} disabled={locked} members={draft.members} memberSegment={draft.segment} onToggle={toggle} />
    <GroupResults id={id} groups={groups.data ?? []} selected={analysis} onSelect={setAnalysis} revision={revision + settingsRevision} disabled={locked} />
  </>
}