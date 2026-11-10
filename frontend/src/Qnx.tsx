import { useState } from 'react'
import type { RefObject } from 'react'
import { api, apiResponse, downloadFilename, formatNumber } from './api'
import { useEditGuard, useMutation } from './Editing'

type Capture = {
  capture_id: string; serial?: string; qnx_address?: string; title?: string
  remote_session_id?: string; status: string; running: boolean | null; state_corrupt: boolean
  warning?: string; raw_only?: boolean; can_import: boolean; archive_available: boolean
  error?: string; missing_modules?: string[]; alignment_error_ms?: number | null
}
type RemoteSession = { remote_session_id: string; status: string; has_metadata: boolean }
type Action = 'captures' | 'sessions' | 'status' | 'refresh' | 'start' | 'recover' | 'stop' | 'pull' | 'import' | 'archive'
type Props = {
  serial: string; connected: boolean; locked: boolean; visible: boolean
  running: RefObject<boolean>; polling: RefObject<Promise<void> | null>; topPolling: RefObject<Promise<void> | null>
  onBusy: (label: string) => void; onImported: (id: string) => void
}

function validAddress(address: string) {
  if (!address || address.length > 253 || address !== address.trim()) return false
  if (address.includes(':')) {
    if (!/^[0-9a-fA-F:.]+$/.test(address)) return false
    try { return Boolean(new URL(`http://[${address}]/`).hostname) } catch { return false }
  }
  if (/^[0-9.]+$/.test(address)) {
    const parts = address.split('.')
    return parts.length === 4 && parts.every((part) => /^(0|[1-9][0-9]{0,2})$/.test(part) && Number(part) <= 255)
  }
  return address.split('.').every((part) => /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(part))
}

function stateLabel(capture: Capture) {
  if (capture.state_corrupt) return '状态损坏，运行状态未知'
  if (capture.running === true) return '采集中（最后已知）'
  if (capture.running === false) return '已停止（最后已知）'
  return '运行状态未知，不能视为已停止'
}

export function Qnx({ serial, connected, locked, visible, running, polling, topPolling, onBusy, onImported }: Props) {
  const mutation = useMutation()
  const guard = useEditGuard()
  const [address, setAddress] = useState('192.168.1.1')
  const [title, setTitle] = useState('')
  const [intervalMs, setIntervalMs] = useState(1000)
  const [duration, setDuration] = useState(60)
  const [remoteId, setRemoteId] = useState('')
  const [recoverStop, setRecoverStop] = useState(false)
  const [captures, setCaptures] = useState<Capture[]>([])
  const [capture, setCapture] = useState<Capture | null>(null)
  const [remote, setRemote] = useState<{ serial: string; address: string; sessions: RemoteSession[] } | null>(null)
  const [notice, setNotice] = useState('')
  const [expanded, setExpanded] = useState(false)
  const [unlocking, setUnlocking] = useState(false)
  const [password, setPassword] = useState('')
  const [unlockError, setUnlockError] = useState('')
  const blocked = locked || mutation.busy
  const targetReady = connected && Boolean(serial) && validAddress(address)
  const remoteReady = connected && capture?.serial === serial && !capture?.state_corrupt
  const settingsValid = Number.isInteger(intervalMs) && intervalMs >= 100 && intervalMs <= 60000
    && Number.isInteger(duration) && duration >= 1 && duration <= 3600
  const allowed: Record<Action, boolean> = {
    captures: true, sessions: targetReady, start: targetReady && settingsValid,
    recover: targetReady && /^[a-f0-9]{32}$/.test(remoteId),
    status: Boolean(capture && !capture.state_corrupt), refresh: Boolean(capture && remoteReady),
    stop: Boolean(capture && remoteReady), pull: Boolean(capture && remoteReady),
    import: Boolean(capture?.can_import && !capture.state_corrupt), archive: Boolean(capture?.archive_available),
  }
  function remember(next: Capture) {
    setCapture(next)
    setCaptures((previous) => [next, ...previous.filter((item) => item.capture_id !== next.capture_id)])
  }
  async function perform(action: Action) {
    if (!expanded || blocked || running.current || !allowed[action]) return
    if (action === 'start' && !window.confirm(`将在 ADB 设备 ${serial} 经 SSH 访问 QNX ${address}，开始 ${duration} 秒采集。启动前会正常停止同一 QNX 上本工具的旧采集，可能由其他主机发起；无法确认安全停止则不启动。不强杀进程、不删除日志。关闭页面或退出本地服务不会停止采集。确认开始？`)) return
    if (action === 'stop' && !window.confirm(`确认正常停止 QNX ${capture?.qnx_address} 的任务 ${capture?.remote_session_id || capture?.capture_id} 并自动拉取日志？不强杀、不删除；停止未确认时不能视为已停止。`)) return
    if (action === 'recover' && !window.confirm(`确认恢复 QNX ${address} 的远端会话 ${remoteId} 到本地？${recoverStop ? '将正常停止该任务并拉取日志，不强杀、不删除。' : '仅恢复并拉取，不停止运行中的任务；运行中可能无法拉取，可稍后重试。'}缺少时钟锚时只能下载原始归档，不能导入分析。`)) return
    if (action === 'import' && !guard.confirm('导入 QNX 本地日志后将切换分析会话并放弃未保存配置，是否继续？')) return
    await mutation.run(async (signal) => {
      running.current = true
      onBusy(`qnx-${action}`)
      setNotice('')
      try {
        await Promise.all([polling.current, topPolling.current])
        signal.throwIfAborted()
        if (action === 'captures') {
          const result = await api<{ captures: Capture[] }>('/api/qnx/captures', { signal })
          if (signal.aborted) return
          setCaptures(result.captures)
          setCapture((previous) => result.captures.find((item) => item.capture_id === previous?.capture_id) ?? result.captures[0] ?? null)
          setNotice(result.captures.length ? '本地历史已读取；未查询远端，运行状态仅为最后已知。' : '暂无本地 QNX 采集记录，可新建或恢复远端会话。')
        } else if (action === 'sessions') {
          setRemote(null)
          const result = await api<{ sessions: RemoteSession[] }>(`/api/qnx/sessions?${new URLSearchParams({ serial, qnx_address: address })}`, { signal })
          if (!signal.aborted) { setRemote({ serial, address, sessions: result.sessions }); setNotice('远端会话查询完成，状态为查询时快照。') }
        } else if (action === 'status' || action === 'refresh') {
          const query = new URLSearchParams({ capture_id: capture!.capture_id })
          if (action === 'refresh') query.set('refresh', 'true')
          const result = await api<Capture>(`/api/qnx/status?${query}`, { signal })
          if (!signal.aborted) { remember(result); setNotice(action === 'refresh' ? '已显式查询远端；请以运行状态与警告为准。' : '已读取本地记录，未访问远端。') }
        } else if (action === 'archive') {
          const response = await apiResponse(`/api/qnx/archive?${new URLSearchParams({ capture_id: capture!.capture_id })}`, { signal })
          const filename = downloadFilename(response, '.tar')
          const blob = await response.blob()
          if (signal.aborted) return
          if (!blob.size) throw new Error('本地归档为空，请重新拉取。')
          const url = URL.createObjectURL(blob)
          const link = document.createElement('a')
          link.href = url
          link.download = filename
          document.body.append(link)
          link.click()
          link.remove()
          window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
          setNotice('QNX 本地原始归档已触发浏览器下载。')
        } else {
          const body = action === 'start'
            ? { confirm: true, serial, qnx_address: address, interval_ms: intervalMs, max_duration_s: duration, title }
            : action === 'recover' ? { confirm: true, serial, qnx_address: address, remote_session_id: remoteId, stop: recoverStop, title }
              : { confirm: true, capture_id: capture!.capture_id }
          const init = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }
          if (action === 'import') {
            const result = await api<{ capture: Capture; session_id: string; duplicate: boolean }>('/api/qnx/import', init)
            if (signal.aborted) return
            remember(result.capture)
            setNotice(result.duplicate ? '已打开已有 QNX 分析会话，本地归档保留。' : 'QNX 本地日志导入完成，归档保留。')
            onImported(result.session_id)
          } else {
            const result = await api<Capture>(`/api/qnx/${action}`, init)
            if (signal.aborted) return
            remember(result)
            setNotice(action === 'start' ? '启动请求已完成，关闭页面不会停止采集；请显式刷新远端状态。' : '操作已返回，请核对状态、警告与本地数据可用性；不会自动导入。')
          }
        }
      } finally {
        running.current = false
        if (!signal.aborted) onBusy('')
      }
    })
  }
  return <section hidden={!visible} className="panel device-panel" aria-label="QNX 采集">
    <div className="section-heading">
      <div><h2>QNX 采集</h2><p>内部试用，暂不对外开放。展开需输入访问密码。</p></div>
      <button type="button" disabled={blocked} aria-expanded={expanded} aria-controls="qnx-capture-content" onClick={() => {
        setExpanded(false)
        setUnlocking(!expanded && !unlocking)
        setPassword('')
        setUnlockError('')
      }}>{expanded ? '折叠并锁定' : unlocking ? '取消展开' : '展开 QNX 采集'}</button>
    </div>
    {!expanded && unlocking && <form onSubmit={(event) => {
      event.preventDefault()
      if (blocked) return
      // 临时界面入口限制，不替代后端鉴权；不持久化密码或解锁状态。
      if (password !== '666888') {
        setPassword('')
        setUnlockError('密码错误，请重新输入。')
        return
      }
      setPassword('')
      setUnlockError('')
      setUnlocking(false)
      setExpanded(true)
    }}>
      <div className="device-controls">
        <label>访问密码<input type="password" value={password} autoComplete="off" disabled={blocked} required
          aria-invalid={Boolean(unlockError)} aria-describedby={unlockError ? 'qnx-unlock-error' : undefined}
          onChange={(event) => { setPassword(event.target.value); setUnlockError('') }} /></label>
      </div>
      <button type="submit" disabled={blocked || !password}>确认展开</button>
      {unlockError && <p id="qnx-unlock-error" className="error" role="alert">{unlockError}</p>}
    </form>}
    <div id="qnx-capture-content" hidden={!expanded}>
    {expanded && <>
    <p>经上方所选 ADB 设备访问 QNX：{serial || '未选择设备'}。折叠或关闭页面不停止远端采集。</p>
    <div className="device-controls">
      <label>标题（可选）<input value={title} maxLength={120} disabled={blocked} onChange={(event) => setTitle(event.target.value)} /></label>
      <label>采样间隔（毫秒）<input type="number" min={100} max={60000} step={1} value={intervalMs} disabled={blocked} onChange={(event) => setIntervalMs(Number(event.target.value))} /></label>
      <label>最长时长（秒）<input type="number" min={1} max={3600} step={1} value={duration} disabled={blocked} onChange={(event) => setDuration(Number(event.target.value))} /></label>
    </div>
    <details>
      <summary>高级设置 · QNX 地址：{address || '未填写'}</summary>
      <div className="device-controls">
        <label>QNX 地址<input value={address} disabled={blocked} placeholder="IP 地址或主机名，不含协议与端口" onChange={(event) => { setAddress(event.target.value); setRemote(null) }} /></label>
      </div>
      <p className="muted">默认使用 192.168.1.1，仅在设备实际地址不同时修改。</p>
    </details>
    {!validAddress(address) && <p className="error">请在高级设置中填写有效的 QNX IP 地址或主机名，不允许协议、路径、端口或空格。</p>}
    {!settingsValid && <p className="error">间隔须为 100–60000 毫秒整数，时长须为 1–3600 秒整数。</p>}
    <div className="actions device-actions"><button className="primary" disabled={blocked || !allowed.start} onClick={() => void perform('start')}>开始 QNX 采集</button><button disabled={blocked || !allowed.sessions} onClick={() => void perform('sessions')}>查询远端会话</button></div>
    <p className="muted">启动会正常停止同 QNX 上本工具的旧采集（可能由其他主机启动），不强杀、不删除。默认只读本地记录，不自动查询 QNX；操作期间暂停 SysMonitor / Top 状态轮询。</p>
    <details><summary>按远端 ID 恢复采集</summary>
      {remote && remote.serial === serial && remote.address === address && <div>
        <p>远端查询快照：{remote.sessions.length} 个会话</p>
        {remote.sessions.map((item) => <p key={item.remote_session_id}><button disabled={blocked} onClick={() => setRemoteId(item.remote_session_id)}>选择 {item.remote_session_id}</button> · {item.status === 'running' ? '采集中' : item.status === 'stopped' ? '已停止' : '状态未知'} · {item.has_metadata ? '有元数据' : '缺少元数据，可能仅能恢复原始数据'}</p>)}
      </div>}
      <div className="device-controls"><label>远端会话 ID<input value={remoteId} maxLength={32} disabled={blocked} placeholder="32 位小写十六进制 ID" onChange={(event) => setRemoteId(event.target.value)} /></label><label className="device-check"><input type="checkbox" checked={recoverStop} disabled={blocked} onChange={(event) => setRecoverStop(event.target.checked)} />恢复时正常停止并拉取（默认不停止）</label></div>
      <button disabled={blocked || !allowed.recover} onClick={() => void perform('recover')}>确认恢复远端会话</button>
    </details>
    <div className="section-heading"><h3>本地采集历史与归档</h3><button disabled={blocked} onClick={() => void perform('captures')}>读取 / 刷新本地历史</button></div>
    <label>本地任务<select value={capture?.capture_id || ''} disabled={blocked} onChange={(event) => { setCapture(captures.find((item) => item.capture_id === event.target.value) || null); mutation.setError(''); setNotice('') }}><option value="">请读取历史或开始采集</option>{captures.map((item) => <option key={item.capture_id} value={item.capture_id}>{item.title || item.qnx_address || 'QNX'} · {item.capture_id} · {stateLabel(item)}</option>)}</select></label>
    <div className="actions device-actions">
      {([['status', '读取本地状态'], ['refresh', '显式刷新远端状态'], ['stop', '停止并拉取'], ['pull', '仅拉取（不停止）'], ['import', '导入本地日志并分析'], ['archive', '下载本地原始归档']] as const).map(([action, label]) => <button key={action} disabled={blocked || !allowed[action]} onClick={() => void perform(action)}>{label}</button>)}
    </div>
    <p className="muted">历史、本地导入和归档下载无需设备在线。远端操作使用所选历史任务保存的地址，并要求上方选择同一 ADB 设备。仅拉取不会停止运行者；停止操作自动拉取，但不自动导入。</p>
    {mutation.busy && <p role="status">正在执行 QNX 操作，请勿关闭页面；远端状态尚未确认。</p>}
    {mutation.error && <p className="error" role="alert">{mutation.error} 当前远端状态未确认，不代表采集已停止；可刷新本地历史找回任务或按远端 ID 恢复。</p>}
    {notice && <p className="device-notice" role="status">{notice}</p>}
    {capture && <div className="device-status">
      <h3>{stateLabel(capture)}</h3>
      <p>本地任务：{capture.capture_id} · 远端会话：{capture.remote_session_id || '未知'}</p>
      <p>绑定设备：{capture.serial || '未知'} · QNX：{capture.qnx_address || '未知'} · 原始状态：{capture.status}</p>
      <p>本地归档：{capture.archive_available ? '可下载' : '不可用'} · 分析导入：{capture.can_import ? '可导入' : '不可用'}</p>
      {capture.raw_only && <p className="error">仅支持原始归档：缺少时钟锚或数据不满足导入条件，不会伪造时间对齐。</p>}
      {capture.warning && <p className="error">{capture.warning}</p>}
      {capture.error && <p className="error">{capture.error}</p>}
      {capture.missing_modules?.length ? <p>缺失模块：{capture.missing_modules.join('、')}</p> : null}
      {capture.alignment_error_ms != null && <p>粗时间对齐误差：{formatNumber(capture.alignment_error_ms)} 毫秒</p>}
    </div>}
    <p className="muted">QNX 分析为静态 SVG 报告，不支持缩放。保留日志原单位；CPU 已是 8 核均值，不再除以核数；DDR 使用平台原始口径，模块间仅作粗时间对齐。</p>
    </>}
    </div>
  </section>
}