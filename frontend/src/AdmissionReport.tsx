import { useEffect, useEffectEvent, useId, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { api, apiResponse, errorMessage, formatTime, sessionPath } from './api'
import type { Overview } from './api'
import { useDirty, useEditGuard } from './Editing'
import './AdmissionReport.css'

type Scene = 'background' | 'foreground' | 'unknown'
type Placement = 'personal' | 'child' | 'sibling'
type State = 'generating' | 'ready' | 'publishing' | 'published' | 'unknown' | 'failed'
type Draft = { draft_id: string; state: State; title: string; warnings: string[]; error?: string; link?: string }
type Recovery = { draft_id: string; attempted: boolean; unknown: boolean }
const states: Record<State, string> = { generating: '本地生成中', ready: '可预览 / 待发布', publishing: '发布中', published: '已发布', unknown: '发布结果未知', failed: '失败' }
const validId = (value: string) => /^[a-f0-9]{32}$/.test(value)
const safeLink = (value?: string) => value && /^https:\/\/joyspace\.jd\.com\/pages\/[A-Za-z0-9_-]{1,128}\/?$/.test(value) ? value : undefined
function parseRecovery(raw: string | null): Recovery[] {
  const value: unknown = raw === null ? [] : JSON.parse(raw)
  if (!Array.isArray(value) || !value.every((item) => item && typeof item === 'object' && !Array.isArray(item) && typeof item.draft_id === 'string' && validId(item.draft_id) && typeof item.attempted === 'boolean' && typeof item.unknown === 'boolean')) throw new Error('恢复日志格式无效')
  return value
}
function readRecovery(key: string): { records: Recovery[]; raw: string | null } | null {
  try {
    const raw = localStorage.getItem(key)
    return { records: parseRecovery(raw), raw }
  } catch { return null }
}
function mergeRecovery(...groups: Recovery[][]): Recovery[] {
  const merged = new Map<string, Recovery>()
  for (const item of groups.flat()) {
    const old = merged.get(item.draft_id)
    merged.set(item.draft_id, { draft_id: item.draft_id, attempted: !!old?.attempted || item.attempted, unknown: !!old?.unknown || item.unknown })
  }
  return [...merged.values()]
}
function metadata(value: Draft, expected?: string): Draft {
  if (!value || typeof value.draft_id !== 'string' || !validId(value.draft_id) || (expected && value.draft_id !== expected) || !Object.hasOwn(states, value.state) || typeof value.title !== 'string' || !Array.isArray(value.warnings) || !value.warnings.every((item) => typeof item === 'string') || (value.error !== undefined && typeof value.error !== 'string') || (value.link !== undefined && typeof value.link !== 'string')) throw new Error('草稿状态响应无效，请查询原草稿。')
  return value
}

class AdmissionRejected extends Error {}

// The shared API helper discards structured errors. Only an explicit rejection
// from the admission POST endpoint can unlock the corresponding submission.
async function admissionPost(path: string, body: object, signal: AbortSignal, beforePost: () => void): Promise<Draft> {
  const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(300_000)])
  const init = { signal: requestSignal, cache: 'no-store', credentials: 'same-origin', redirect: 'error' } as const
  const tokenResponse = await fetch('/api/token', init)
  if (!tokenResponse.ok) throw new Error('无法获取本地会话令牌，请保留编号并查询。')
  const { token } = await tokenResponse.json() as { token?: unknown }
  if (typeof token !== 'string' || !token) throw new Error('本地服务未返回有效会话令牌。')
  beforePost() // Recheck after the asynchronous token request, immediately before POST.
  const response = await fetch(path, {
    ...init, method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-Token': token }, body: JSON.stringify(body),
  })
  if (response.ok) return response.json() as Promise<Draft>
  let payload: { detail?: unknown; accepted?: unknown } | null = null
  try { payload = await response.json() as { detail?: unknown; accepted?: unknown } } catch { /* Keep the recovery lock without structured rejection. */ }
  const message = `请求失败（HTTP ${response.status}）${typeof payload?.detail === 'string' ? `：${payload.detail}` : ''}`
  if (response.status >= 400 && response.status < 500 && payload?.accepted === false) throw new AdmissionRejected(message)
  throw new Error(message)
}

export function AdmissionReport({ id, name, segments, disabled }: { id: string; name: string; segments: Overview['segments']; disabled: boolean }) {
  const path = `${sessionPath(id)}/admission/drafts`
  const storageKey = `admission-recovery:${id}`
  const [initialRecovery] = useState(() => readRecovery(storageKey))
  const [recovery, setRecovery] = useState<Recovery[]>(() => mergeRecovery(initialRecovery?.records || []))
  const journal = useRef(recovery)
  const observedStorage = useRef(initialRecovery?.raw ?? null)
  const recoveryFault = useRef(initialRecovery === null)
  const [storageBlocked, setStorageBlocked] = useState(initialRecovery === null)
  const storageRevision = useRef(0)
  const listedRef = useRef(false)
  const [drafts, setDrafts] = useState<Draft[]>([])
  const [selected, setSelected] = useState(recovery[0]?.draft_id || '')
  const [open, setOpen] = useState(false)
  const [title, setTitle] = useState([...`${name}性能准入报告`].slice(0, 200).join(''))
  const [scenes, setScenes] = useState<Record<number, Scene | ''>>({})
  const [factor, setFactor] = useState('')
  const [dirty, setDirty] = useState(false)
  const [placement, setPlacement] = useState<Placement | ''>('')
  const [parent, setParent] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [requesting, setRequesting] = useState(false)
  const [requestRevision, setRequestRevision] = useState(0)
  const [listed, setListedState] = useState(false)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState('')
  const [previewLoaded, setPreviewLoaded] = useState(false)
  const previewRef = useRef('')
  const controller = useRef<AbortController | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const guard = useEditGuard()
  const guardKey = useId()
  const headingId = useId()
  const draft = drafts.find((item) => item.draft_id === selected)
  const active = drafts.some((item) => item.state === 'generating' || item.state === 'publishing')
  const busy = requesting || active
  // The same session may be generated and published repeatedly. Only in-flight
  // work and an unusable recovery journal still gate writes.
  const blocked = storageBlocked || active
  const attempted = recovery.find((item) => item.draft_id === selected)?.attempted
  const unknown = draft?.state !== 'published' && (draft?.state === 'unknown' || recovery.find((item) => item.draft_id === selected)?.unknown)
  const canClose = !requesting && (!active || !!error)
  useDirty(open && dirty)
  const updateGuard = guard.update
  useLayoutEffect(() => { updateGuard(guardKey, { dirty: false, busy:open && busy }); return () => updateGuard(guardKey) }, [open, busy, requestRevision, guardKey, updateGuard])
  useEffect(() => () => { controller.current?.abort(); if (previewRef.current) URL.revokeObjectURL(previewRef.current) }, [])
  useEffect(() => {
    if (open) dialog.current?.showModal()
    else if (dialog.current?.open) { dialog.current.close(); trigger.current?.focus() }
  }, [open])

  function clearPreview() {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current)
    previewRef.current = ''
    setPreview(''); setPreviewLoaded(false)
  }
  function resetPublish() { setPlacement(''); setParent(''); setConfirmed(false); clearPreview() }
  function setListed(value: boolean) { listedRef.current = value; setListedState(value) }
  function invalidateRecovery() {
    storageRevision.current += 1
    setListed(false); resetPublish()
  }
  function blockStorage() {
    if (!recoveryFault.current) invalidateRecovery()
    recoveryFault.current = true; setStorageBlocked(true); setListed(false)
  }
  function syncRecovery() {
    const latest = readRecovery(storageKey)
    if (!latest) { blockStorage(); setRecovery(journal.current); return }
    if (latest.raw !== observedStorage.current) {
      invalidateRecovery()
      if (latest.raw === null && observedStorage.current !== null) blockStorage()
      observedStorage.current = latest.raw
    }
    journal.current = mergeRecovery(journal.current, latest.records)
    setRecovery(journal.current)
  }
  function persistRecovery(next: Recovery[], required = false) {
    if (!recoveryFault.current) {
      try {
        const raw = JSON.stringify(next)
        if (raw !== observedStorage.current) localStorage.setItem(storageKey, raw)
        observedStorage.current = raw
      } catch { blockStorage() }
    }
    if (required && recoveryFault.current) throw new Error('恢复日志读取、校验或保存失败，本窗口已禁止生成和发布；仍可查询草稿及查看预览。')
  }
  function remember(draftId: string, patch: Partial<Pick<Recovery, 'attempted' | 'unknown'>> = {}, required = false) {
    syncRecovery()
    // Merge safety flags monotonically, including when persistence fails.
    journal.current = mergeRecovery(journal.current, [{ draft_id: draftId, attempted: patch.attempted === true, unknown: patch.unknown === true }])
    setRecovery(journal.current)
    // Only recovery IDs and safety flags are persisted, never authentication or report data.
    persistRecovery(journal.current, required)
  }
  function releaseRejected(draftId: string, revision: number, created = false) {
    syncRecovery()
    // Only explicit accepted:false may release this request's ID/attempted flag.
    // Any observed concurrent change keeps the lock conservatively.
    if (recoveryFault.current || revision !== storageRevision.current) return
    const record = journal.current.find((item) => item.draft_id === draftId)
    if (record?.unknown || (created && record?.attempted)) return
    const next = created ? journal.current.filter((item) => item.draft_id !== draftId)
      : journal.current.map((item) => item.draft_id === draftId ? { ...item, attempted: false } : item)
    persistRecovery(next, true)
    journal.current = next; setRecovery(next)
    if (created) {
      setDrafts((previous) => previous.filter((item) => item.draft_id !== draftId))
      setSelected(next[0]?.draft_id || ''); setDirty(true)
    }
  }
  function accept(value: Draft, expected?: string) {
    const result = metadata(value, expected)
    remember(result.draft_id, {
      attempted: ['publishing', 'published', 'unknown'].includes(result.state),
      unknown: result.state === 'unknown',
    })
    setDrafts((previous) => [result, ...previous.filter((item) => item.draft_id !== result.draft_id)])
    return result
  }
  const onRecoveryStorage = useEffectEvent((event: StorageEvent) => {
    if (event.key !== storageKey && event.key !== null) return
    // Keep event records as well as the latest value: a later deletion must
    // not erase a lock that was visible in an earlier queued event.
    invalidateRecovery()
    try {
      if (event.oldValue !== null) journal.current = mergeRecovery(journal.current, parseRecovery(event.oldValue))
    } catch { blockStorage() }
    try {
      if (event.newValue === null) blockStorage()
      else journal.current = mergeRecovery(journal.current, parseRecovery(event.newValue))
    } catch { blockStorage() }
    syncRecovery()
  })
  useEffect(() => {
    const onStorage = (event: StorageEvent) => onRecoveryStorage(event)
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [storageKey])
  function assertWrite(revision?: number) {
    syncRecovery()
    // Repeat generation and publishing are allowed; only a broken recovery
    // journal, a stale draft list, or in-flight work may block a write.
    if (recoveryFault.current || !listedRef.current || active || (revision !== undefined && revision !== storageRevision.current)) throw new Error('恢复记录或安全门禁已变化，请刷新草稿列表后重新预览并确认；本次不会发送 POST。')
    // localStorage read/merge/write is not an atomic cross-tab transaction.
  }
  async function run(action: (signal: AbortSignal) => Promise<void>, write = false) {
    if (controller.current || (write && !guard.acquire(guardKey))) return
    const request = new AbortController()
    controller.current = request
    setRequesting(true); setError('')
    try { await action(request.signal) }
    catch (failure) {
      if (!request.signal.aborted) setError(failure instanceof AdmissionRejected
        ? `${errorMessage(failure)} 服务端明确未受理本次请求；请刷新草稿列表后检查配置，重新预览并确认发布，不会自动重发。`
        : `${errorMessage(failure)} 已建草稿和编号保留；请查询状态，不会自动重发生成或发布请求。关闭窗口或断开请求不代表撤回。`)
    }
    finally {
      if (controller.current === request) {
        controller.current = null
        if (!request.signal.aborted) {
          // Reconcile the synchronous lock even if React batches a preflight
          // failure's requesting=true/false into a single render.
          setRequestRevision((revision) => revision + 1)
          setRequesting(false)
        }
      }
    }
  }
  async function query(draftId: string, signal: AbortSignal) {
    try {
      const result = await api<Draft>(`${path}/${draftId}`, { signal })
      signal.throwIfAborted()
      accept(result, draftId)
    } catch (failure) {
      // A later preview request must not hide a query failure and enable writes
      // from stale ready metadata. Restore the gate only after a full refresh.
      if (!signal.aborted) setListed(false)
      throw failure
    }
  }
  function refresh() {
    if (controller.current) return
    resetPublish(); setListed(false); syncRecovery()
    void run(async (signal) => {
      const revision = storageRevision.current
      const result = await api<{ drafts: Draft[] }>(path, { signal })
      signal.throwIfAborted()
      if (!Array.isArray(result.drafts)) throw new Error('草稿列表格式无效')
      result.drafts.forEach((item) => metadata(item))
      result.drafts.forEach((item) => accept(item))
      const next = selected || journal.current[0]?.draft_id || ''
      if (next) { setSelected(next); resetPublish(); await query(next, signal) }
      syncRecovery()
      setListed(!recoveryFault.current && revision === storageRevision.current)
    })
  }
  function show() {
    if (disabled || guard.busy) return
    setTitle([...`${name}性能准入报告`].slice(0, 200).join('')); setScenes({}); setFactor(''); setDirty(false)
    resetPublish(); setOpen(true); setListed(false); refresh()
  }
  function close() {
    if (!canClose || controller.current) return
    const unresolved = blocked || !!error
    if ((unresolved || dirty) && !window.confirm(unresolved
      ? '确认关闭？恢复编号将保留，后台任务可能仍在运行；关闭不会取消或撤回发布。重新打开只查询原草稿，不会重发。未生成配置将放弃。'
      : '放弃未生成的配置？已建草稿会保留，关闭不撤回发布。')) return
    setOpen(false); setDirty(false); resetPublish()
  }
  // Poll only GET. A failed query stops polling and leaves a manual recovery button.
  const pollDrafts = useEffectEvent(() => {
    void run(async (signal) => {
      for (const item of drafts.filter((entry) => ['generating', 'publishing'].includes(entry.state))) await query(item.draft_id, signal)
    })
  })
  useEffect(() => {
    if (!open || !active || requesting || error) return
    const timer = window.setTimeout(() => pollDrafts(), 2000)
    return () => window.clearTimeout(timer)
  }, [open, active, requesting, error, drafts])

  function generate(event: FormEvent) {
    event.preventDefault()
    if (busy || guard.busy || blocked || !listed) return
    const number = factor.trim() === '' ? null : Number(factor)
    if (!title.trim() || [...title].length > 200 || title.includes('\0') || (number !== null && (!Number.isFinite(number) || number <= 0))) {
      setError('请填写不含空字符且不超过200字符的标题；系数只能留空或填写正有限数。'); return
    }
    // Scenes are optional: unselected segments follow the 关注进程 scene labels.
    const chosen = Object.fromEntries(Object.entries(scenes).filter(([, value]) => value))
    void run(async (signal) => {
      assertWrite()
      const revision = storageRevision.current
      const draftId = crypto.randomUUID().replaceAll('-', '')
      remember(draftId, {}, true) // Persist before POST so a refresh cannot lose an ambiguous request.
      setSelected(draftId); resetPublish(); setDirty(false)
      try {
        const result = await admissionPost(path, { draft_id: draftId, title: title.trim(), factor: number, ...(Object.keys(chosen).length ? { scenes: chosen } : {}) }, signal, () => assertWrite(revision))
        signal.throwIfAborted(); accept(result, draftId)
      } catch (failure) {
        signal.throwIfAborted()
        if (failure instanceof AdmissionRejected) {
          setListed(false); releaseRejected(draftId, revision, true)
          throw failure
        }
        try { await query(draftId, signal) } catch { signal.throwIfAborted(); throw failure }
      }
    }, true)
  }
  function loadPreview() {
    if (!draft || requesting || active) return
    const draftId = draft.draft_id
    clearPreview(); setConfirmed(false); syncRecovery()
    void run(async (signal) => {
      const revision = storageRevision.current
      const response = await apiResponse(`${path}/${draftId}/preview`, { signal })
      if (response.headers.get('content-type')?.split(';')[0]?.trim().toLowerCase() !== 'text/html') throw new Error('本地预览格式无效')
      const blob = await response.blob()
      signal.throwIfAborted()
      if (!blob.size) throw new Error('本地预览为空')
      syncRecovery()
      if (revision !== storageRevision.current) throw new Error('恢复记录已变化，请刷新列表后重新加载预览。')
      const url = URL.createObjectURL(blob)
      previewRef.current = url; setPreview(url)
    })
  }
  const parentValid = /^[A-Za-z0-9_-]{1,128}$/.test(parent.trim()) || !!safeLink(parent.trim())
  const republish = !!draft && draft.state !== 'ready'
  const canPublish = !!draft && ['ready', 'published', 'unknown'].includes(draft.state) && !blocked && listed && !error && !busy && !!preview && previewLoaded && !!placement && (placement === 'personal' || parentValid) && confirmed
  function publish() {
    if (!canPublish || !draft || guard.busy) return
    const draftId = draft.draft_id
    if (!window.confirm(`确认发布“${draft.title}”？将外发完整模板、指定章节统计与趋势 PNG，其他章节需人工填写。不会修改原模板。位置：${placement === 'personal' ? '个人空间' : `${placement === 'child' ? '目标页面的子页' : '目标页面的同级页'} · ${parent.trim()}`}。${republish ? '该草稿此前已发布或结果未知，本次将在 JoySpace 新建一个页面，不会覆盖或删除之前的页面。' : ''}断开请求不代表撤回。`)) return
    void run(async (signal) => {
      assertWrite()
      const revision = storageRevision.current
      remember(draftId, { attempted: true }, true)
      setConfirmed(false); setPlacement(''); setParent('')
      try {
        const result = await admissionPost(`${path}/${draftId}/publish`, { placement, parent_page: placement === 'personal' ? null : parent.trim(), confirmed: true }, signal, () => assertWrite(revision))
        signal.throwIfAborted(); accept(result, draftId)
      } catch (failure) {
        signal.throwIfAborted()
        if (failure instanceof AdmissionRejected) {
          // This request was never accepted. Keep the draft, release only its
          // attempted flag, and require a fresh query, preview and confirmation.
          setListed(false); resetPublish()
          releaseRejected(draftId, revision)
          throw failure
        }
        remember(draftId, { unknown: true })
        try { await query(draftId, signal) } catch { signal.throwIfAborted(); throw failure }
      }
    }, true)
  }

  return <>
    <button ref={trigger} disabled={disabled} onClick={show}>生成性能准入报告</button>
    {createPortal(<dialog ref={dialog} className="admission-dialog" aria-labelledby={headingId} onKeyDown={(event)=> { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() } }} onCancel={(event) => { event.preventDefault(); event.stopPropagation(); close() }}>
      <div className="admission-heading"><h2 id={headingId}>生成性能准入报告</h2><button type="button" disabled={!canClose} onClick={close} aria-label="关闭性能准入报告">关闭</button></div>
      <div className="admission-content" aria-busy={requesting}>
        <p className="banner info">生成仅只读获取模板并使用本地统计，不发布、不修改模板。仅填充指定章节统计与趋势 PNG；其他章节由人工填写。</p>
        <form onSubmit={generate}>
          <fieldset disabled={busy} className="config-fields">
            <div className="form-grid">
              <label>报告标题<input required value={title} onChange={(event) => { setTitle([...event.target.value].slice(0, 200).join('')); setDirty(true) }} /><small>最多200字符。</small></label>
              <label>单核 KDMIPS 系数（可选）<input inputMode="decimal" value={factor} onChange={(event) => { setFactor(event.target.value); setDirty(true) }} placeholder="留空沿用离线报告默认 28.75" /><small>必须为正有限数。留空则与离线报告使用同一默认系数，K 列不会留空。</small></label>
            </div>
            <div className="admission-table-scroll" tabIndex={0} role="region" aria-label="时间段场景配置">
              <table><thead><tr><th>时间段</th><th>起止时间</th><th>记录数</th><th>场景（可选，默认按关注进程标注）</th></tr></thead><tbody>
                {segments.map((item) => <tr key={item.segment}><td>{item.segment}</td><td>{formatTime(item.start)} → {formatTime(item.end)}</td><td>{item.records}</td><td>
                  <select aria-label={`时间段 ${item.segment} 的场景`} value={scenes[item.segment] || ''} onChange={(event) => { setScenes({ ...scenes, [item.segment]: event.target.value as Scene | '' }); setDirty(true) }}>
                    <option value="">自动：按关注进程标注</option><option value="background">后台稳态</option><option value="foreground">前台交互</option><option value="unknown">未知 / 未分类</option>
                  </select>
                </td></tr>)}
              </tbody></table>
            </div>
            <div className="actions config-actions"><button className="primary" type="submit" disabled={blocked || !listed || !segments.length}>仅生成本地草稿</button><span>{dirty ? '配置尚未生成' : '未选择场景的时间段按关注进程的前台/后台标注统计'}</span></div>
          </fieldset>
        </form>
        <section className="admission-section" aria-label="已建草稿">
          <div className="actions config-actions"><h3>草稿恢复</h3><button disabled={requesting} onClick={refresh}>刷新草稿列表</button></div>
          {!recovery.length && <p className="muted">{listed ? '暂无已建草稿。' : '正在读取草稿列表；读取成功前不能生成。'}</p>}
          {recovery.length > 0 && <label className="admission-draft-picker">选择草稿<select value={selected} disabled={requesting || active} onChange={(event) => {
            const draftId = event.target.value
            setSelected(draftId); resetPublish(); void run((signal) => query(draftId, signal))
          }}><option value="" disabled>请选择草稿</option>{recovery.map((item) => {
            const info = drafts.find((entry) => entry.draft_id === item.draft_id)
            return <option key={item.draft_id} value={item.draft_id}>{info?.title || '待恢复草稿'} · {info ? states[info.state] : '需查询'} · {item.draft_id}</option>
          })}</select></label>}
          {selected && <><p className="admission-id">草稿编号：{selected}</p><button disabled={requesting} onClick={() => { void run((signal) => query(selected, signal)) }}>查询该草稿状态</button></>}
          {storageBlocked && <p className="banner error" role="alert">本地恢复日志读取、校验或保存失败，或已被删除。本窗口已关闭生成和发布门禁；内存记录保留，仍可刷新列表、查询状态和查看预览，不会自动重发。</p>}
          {blocked && !storageBlocked && <p className="banner info">有草稿正在本地生成或发布中，请等待本次任务结束后再生成或发布。同一份数据可以多次生成与多次发布。</p>}
          {!listed && !storageBlocked && <p className="muted">写操作需刷新草稿列表后再确认；其他标签页更改恢复日志会清除预览和发布确认。</p>}
          {error && <p className="banner error" role="alert">{error}</p>}
          {draft && <p role="status">当前状态：<strong>{states[draft.state]}</strong> · {draft.title}</p>}
          {draft?.error && <p className="banner error" role="alert">{draft.error}</p>}
          {!!draft?.warnings.length && <ul className="banner info">{draft.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
          {unknown && <p className="banner error admission-unknown" role="alert"><strong>发布结果未知。</strong> 请先到 JoySpace 人工确认是否已创建页面，再决定是否再次发布；再次发布会新建页面，可能产生重复页面。关闭窗口不会撤回。</p>}
          {draft?.state === 'unknown' && !!safeLink(draft.link) && <p className="banner info">查询到同名页面，但正文未经校验：<a href={safeLink(draft.link)} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">打开待人工确认的 JoySpace 页面</a>。确认无误后无需再次发布。</p>}
          {attempted && !unknown && draft?.state !== 'published' && <p className="banner info">该草稿已尝试发布，请先查询结果；确认后可再次发布，不会自动重发。</p>}
          {draft?.state === 'published' && (safeLink(draft.link) ? <p><a href={safeLink(draft.link)} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">打开已发布的 JoySpace 页面</a></p> : <p className="banner error">已发布，但返回链接不符合安全格式，请到 JoySpace 人工确认。</p>)}
        </section>
        {draft && ['ready', 'published', 'unknown', 'failed'].includes(draft.state) && <section className="admission-section" aria-label="本地安全预览与发布">
          <div className="actions config-actions"><h3>本地安全预览</h3><button disabled={busy} onClick={loadPreview}>{preview ? '重新加载预览' : '加载本地预览'}</button></div>
          <p className="muted">预览不授予脚本、同源或导航权限。宽表可在预览内横向滚动；发布确认须等预览加载完成。</p>
          {preview && <div className="admission-preview-scroll"><iframe key={preview} className="admission-preview" title={`${draft.title} · 本地安全预览`} sandbox="" referrerPolicy="no-referrer" src={preview} onLoad={() => { if (previewRef.current === preview) setPreviewLoaded(true) }} /></div>}
          {['ready', 'published', 'unknown'].includes(draft.state) && <fieldset className="config-fields" disabled={busy || !previewLoaded}>
            <div className="form-grid">
              <label>发布位置（每次显式选择）<select value={placement} onChange={(event) => { setPlacement(event.target.value as Placement | ''); setParent(''); setConfirmed(false) }}>
                <option value="" disabled>请选择发布位置</option><option value="personal">个人空间</option><option value="child">指定页面的子页</option><option value="sibling">指定页面的同级页</option>
              </select></label>
              {placement && placement !== 'personal' && <label>目标 JoySpace 页面链接或 ID<input required value={parent} onChange={(event) => { setParent(event.target.value); setConfirmed(false) }} placeholder="https://joyspace.jd.com/pages/页面ID" /><small>页面 ID 最长128字符；仅支持固定域页面链接（可带末尾斜杠）或页面 ID，不会默认发布为模板子页。</small>{parent && !parentValid && <small role="alert">请输入有效的 JoySpace 页面链接或 ID。</small>}</label>}
            </div>
            <p className="banner info">发布将向 JoySpace 外发完整模板、指定章节统计及趋势 PNG，不修改原模板；其余章节需人工填写。{republish && '该草稿已发布过或结果未知，再次发布会新建页面，不会覆盖之前的页面。'}</p>
            <label className="admission-confirm"><input type="checkbox" checked={confirmed} disabled={!placement || (placement !== 'personal' && !parentValid)} onChange={(event) => setConfirmed(event.target.checked)} />我已查看本地预览，并确认上述外发内容与发布位置。</label>
            <button className="primary" disabled={!canPublish} onClick={publish}>{republish ? '再次发布到 JoySpace' : '确认发布到 JoySpace'}</button>
          </fieldset>}
        </section>}
        {requesting && <p role="status">正在处理请求，请勿关闭或切换会话…</p>}
        {active && <p role="status">后台任务可能仍在运行；仅查询状态，不重复提交。查询失败后可确认关闭，恢复编号保留，关闭不代表取消；重新打开只查询。</p>}
      </div>
    </dialog>, document.body)}
  </>
}

