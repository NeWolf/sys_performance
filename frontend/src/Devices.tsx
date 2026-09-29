import { useEffect, useRef, useState } from 'react'
import { api, apiResponse, errorMessage } from './api'
import { useEditGuard, useMutation } from './Editing'

type Device = { serial: string; state: string; model: string }
type Status = { serial: string; pids: string[]; properties: Record<string, string>; files: string; privilege: string; deployed: boolean }
type PullResult = { id: string; duplicate: boolean; archive: string; files: string[] }
type TopStatus = {
  id: string | null; status: string; running: boolean; stopping: boolean; importing: boolean
  count: number; target_count: number; interval: number; serial: string | null
  error: string | null; warning?: string | null; archive: string | null; session_id: string | null
  bytes: number; max_bytes: number; remote_path: string | null
  connected: boolean; connection_error: string | null; remote_cleaned: boolean
  started_at: number | null; ended_at: number | null; elapsed_seconds: number | null
}
const topErrors: Record<string, string> = {
  size_limit: '已达到 1GB 文件上限，采集已停止',
  top_failed_or_timeout: 'Top 执行失败或超时', sample_limit: '单次样本超过限额',
  invalid_top: 'Top 输出无法识别', disk_error: '设备文件写入失败，请检查空间与权限',
  detach_failed: '设备后台任务启动失败', interrupted: '设备任务意外中断',
}
const topLabels: Record<string, string> = {
  idle: '未开始', starting: '启动中', running: '采集中', stopping: '停止中',
  stopped: '已停止', completed: '采集完成', importing: '导入中', imported: '已导入', error: '异常',
}

function formatCaptureDuration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return '未记录'
  const total = Math.max(0, Math.floor(seconds))
  const days = Math.floor(total / 86400)
  const hours = Math.floor(total / 3600) % 24
  const minutes = Math.floor(total / 60) % 60
  return `${days ? `${days} 天 ` : ''}${days || hours ? `${hours} 小时 ` : ''}${minutes} 分 ${total % 60} 秒`
}

export function Devices({ onImported, disabled, view, onAdvanced }: { onImported: (id: string) => void; disabled: boolean; view: string; onAdvanced: () => void }) {
  const [revision, setRevision] = useState(0)
  const [devices, setDevices] = useState<{ data: Device[]; loading: boolean; error: string }>({ data: [], loading: true, error: '' })
  const [serial, setSelected] = useState('')
  const connected = !devices.error && devices.data.some((device) => device.serial === serial && device.state === 'device')
  const [interval, setInterval] = useState(30)
  const [tologcat, setTologcat] = useState(false)
  const [asyncWrite, setAsyncWrite] = useState(false)
  const [busy, setBusy] = useState('')
  const mutation = useMutation()
  const guard = useEditGuard()
  const { error } = mutation
  const [notice, setNotice] = useState('')
  const [feedbackSource, setFeedbackSource] = useState<'sysmonitor' | 'top'>('sysmonitor')
  const [status, setStatus] = useState<Status | null>(null)
  const running = useRef(false)
  const polling = useRef<Promise<void> | null>(null)
  const [pollError, setPollError] = useState('')
  const [topStatus, setTopStatus] = useState<TopStatus | null>(null)
  const [topPollError, setTopPollError] = useState('')
  const [topInterval, setTopInterval] = useState(1)
  const [topCount, setTopCount] = useState(-1)
  const [topTitle, setTopTitle] = useState('')
  const topPolling = useRef<Promise<void> | null>(null)
  const topActive = Boolean(topStatus?.running || topStatus?.stopping || topStatus?.importing)
  const topCurrent = Boolean(serial && topStatus?.serial === serial)
  const topReady = topCurrent && connected && Boolean(topStatus?.connected) && !topPollError
  const validTopSettings = Number.isFinite(topInterval) && topInterval >= 1 && topInterval <= 3600
    && Number.isInteger(topCount) && (topCount === -1 || (topCount >= 1 && topCount <= 100000))
  const locked = Boolean(busy) || disabled
  // 恢复操作以设备列表在线及同设备任务身份为准，状态查询失败不应阻断重试。
  const topRecoverable = topCurrent && connected && Boolean(topStatus?.id) && !topStatus?.importing
  const topAllowed = {
    status: Boolean(serial),
    start: topReady && validTopSettings && !topActive,
    stop: topRecoverable,
    pull: topRecoverable && !topStatus?.remote_cleaned
      && Boolean(topStatus?.bytes || topStatus?.running || topStatus?.stopping || !topReady),
    clear: topReady && Boolean(topStatus?.id) && !topActive && !topStatus?.remote_cleaned,
    import: topCurrent && Boolean(topStatus?.id && topStatus.archive) && !topActive,
    archive: topCurrent && Boolean(topStatus?.id && topStatus.archive) && !topActive,
  }
  const busyMessage = busy === 'top-stop'
    ? '正在确认采集停止（最多约 20 秒），请勿关闭页面…'
    : busy === 'top-pull' ? '正在停止尚未结束的采集并拉取日志，停止失败则不拉取，请勿关闭页面…'
      : busy === 'top-analyze' || busy === 'top-import' ? '日志已保存在本地，正在导入分析，请勿关闭页面…'
        : '正在执行设备操作，请勿关闭页面…'

  useEffect(() => {
    const controller = new AbortController()
    async function refresh() {
      if (!serial || running.current || topPolling.current || polling.current) return
      const request = (async () => {
        try {
          const next = await api<TopStatus>(`/api/top/status?${new URLSearchParams({ serial })}`, { signal: controller.signal })
          if (!controller.signal.aborted) { setTopStatus(next); setTopPollError('') }
        } catch (failure) {
          if (!controller.signal.aborted) setTopPollError(errorMessage(failure))
        }
      })()
      topPolling.current = request
      try { await request } finally { if (topPolling.current === request) topPolling.current = null }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [serial, revision])
  const current = connected && status?.serial === serial ? status : null
  const selectedDevice = devices.data.find((device) => device.serial === serial)
  const disconnected = serial && !devices.loading && !devices.error && (!selectedDevice || selectedDevice.state !== 'device')

  useEffect(() => {
    const controller = new AbortController()
    let inFlight = false
    async function refresh() {
      if (inFlight) return
      inFlight = true
      try {
        const data = await api<Device[]>('/api/adb/devices', { signal: controller.signal })
        if (controller.signal.aborted) return
        setDevices({ data, loading: false, error: '' })
        if (!serial) {
          setSelected(data.find((device) => device.state === 'device')?.serial || '')
          return
        }
        if (!data.some((device) => device.serial === serial && device.state === 'device')) {
          setStatus(null)
          setNotice('')
          setPollError('')
          return
        }
        if (running.current || polling.current || topPolling.current) return
        const request = (async () => {
          try {
            const next = await api<Status>(`/api/adb/status?${new URLSearchParams({ serial })}`, { signal: controller.signal })
            if (!controller.signal.aborted) { setStatus(next); setPollError('') }
          } catch (failure) {
            if (!controller.signal.aborted) {
              setStatus(null)
              setPollError(errorMessage(failure))
              // 状态查询途中也可能拔线，再确认连接，避免保留旧的在线状态。
              try {
                const latest = await api<Device[]>('/api/adb/devices', { signal: controller.signal })
                if (!controller.signal.aborted) setDevices({ data: latest, loading: false, error: '' })
              } catch { /* 原始状态错误已显示，下一轮继续检查连接。 */ }
            }
          }
        })()
        polling.current = request
        try { await request } finally { if (polling.current === request) polling.current = null }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setDevices((previous) => ({ ...previous, loading: false, error: errorMessage(failure) }))
          setStatus(null)
        }
      } finally { inFlight = false }
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [serial, revision])

  async function perform(action: 'status' | 'start' | 'stop' | 'pull' | 'delete-logs') {
    if (!serial || !connected || disabled || running.current) return
    if (action === 'pull' && !guard.confirm('拉取前会先停止尚未结束的采集；成功后将切换会话并放弃未保存配置，是否继续？')) return
    if (action === 'start' && !window.confirm(`将在 ${serial} 开始采集；若未部署，将先自动部署采集程序，已部署则直接使用。使用现有 root / su 权限，持久化属性会影响系统 sysmonitor，关闭页面不会停止采集。确认继续？`)) return
    if (action === 'stop' && !window.confirm('将关闭设备共享采集开关，并终止该路径的测试进程。确认停止？')) return
    if (action === 'delete-logs' && !window.confirm(`将永久删除车机 ${serial} 上 /log/sys/perf/ 中的 perf.log 和 perf.1.log 至 perf.4.log，无法恢复。若正在采集，会先关闭共享采集开关并停止测试进程，停止失败则不删除。尚未拉取的日志将丢失，请先备份；本地已导入的数据和备份不受影响。确认删除？`)) return
    await mutation.run(async (signal) => {
      running.current = true
      setFeedbackSource('sysmonitor')
      setBusy(action)
      setNotice('')
      setPollError('')
      try {
        // 等待已发出的状态查询结束，避免与后端设备操作锁冲突。
        await Promise.all([polling.current, topPolling.current])
        if (signal.aborted) return
        setStatus(null)
        if (action === 'status') {
          const next = await api<Status>(`/api/adb/status?${new URLSearchParams({ serial })}`, { signal })
          if (!signal.aborted) setStatus(next)
        } else {
          const body = action === 'start' ? { serial, confirm: true, interval, tologcat, async_write: asyncWrite } : { serial, confirm: true }
          const init = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }
          if (action === 'pull') {
            const result = await api<PullResult>('/api/adb/pull', init)
            if (signal.aborted) return
            setNotice(`${result.duplicate ? '已打开已有会话' : '拉取并分析完成'}；${result.files.length} 个原始文件保存在：${result.archive}`)
            onImported(result.id)
          } else {
            const next = await api<Status>(`/api/adb/${action}`, init)
            if (signal.aborted) return
            setStatus(next)
            setNotice(action === 'start' ? '采集已开始，状态每 5 秒自动刷新；不会自动清空旧日志。' : action === 'delete-logs' ? '采集已停止，车机性能日志已清空；本地已导入的数据和备份未改动。' : '采集开关已关闭，测试进程已退出。')
          }
        }
      } finally {
        running.current = false
        if (!signal.aborted) { setBusy(''); setRevision((value) => value + 1) }
      }
    })
  }

  async function performTop(action: 'status' | 'start' | 'stop' | 'pull' | 'clear' | 'import' | 'archive') {
    if (locked || running.current || !topAllowed[action]) return
    const captureId = topStatus?.id
    if (action === 'start' && !window.confirm(`将在 ${serial} 推送脚本并开始设备端 Top 采集（${topInterval} 秒，${topCount === -1 ? '持续采集直到手动停止或达到上限' : `${topCount} 次`}）。无需 sysmonitor 或 root，不修改其采集开关。断连、关闭页面或退出本地服务不影响设备采集。原始文件最多 1GB（1,000,000,000 字节），到达上限前停止并保留完整样本。确认开始？`)) return
    if (action === 'stop' && !window.confirm(`确认停止 ${serial} 的 Top 采集？已完成样本将保留，不影响 sysmonitor 采集。`)) return
    if (action === 'pull') {
      const message = `将先安全停止 ${serial} 尚未结束的 Top 采集，确认停止后拉取日志并分析；停止失败则不拉取。成功后将切换会话并放弃未保存配置，设备日志和本地归档均保留。是否继续？`
      if (!guard.confirm(message) || (!guard.dirty && !window.confirm(message))) return
    }
    if (action === 'clear' && !window.confirm(`将永久清空设备 ${serial} 上本工具产生的全部已结束 Top 日志（含历史任务），释放日志空间，保留任务元数据。当前任务：${captureId}。未拉取的日志将无法恢复，请先备份。不会停止正在运行的任务，也不会清理 SysMonitor 日志或本地已拉取、已导入的数据。确认清理？`)) return
    if (action === 'import' && !guard.confirm('导入 Top 日志后将切换分析会话并放弃未保存配置，是否继续？')) return
    await mutation.run(async (signal) => {
      running.current = true
      setFeedbackSource('top')
      setBusy(`top-${action}`)
      setNotice('')
      try {
        await Promise.all([polling.current, topPolling.current])
        if (signal.aborted) return
        if (action === 'status') {
          const next = await api<TopStatus>(`/api/top/status?${new URLSearchParams({ serial })}`, { signal })
          if (!signal.aborted) { setTopStatus(next); setTopPollError('') }
        } else if (action === 'archive') {
          const response = await apiResponse(`/api/top/archive?${new URLSearchParams({ capture_id: captureId! })}`, { signal })
          const filename = response.headers.get('Content-Disposition')?.match(/filename="?(Top\d{14}\.txt)"?(?:;|$)/)?.[1]
          if (!filename) throw new Error('归档文件名无效，请更新程序后重试下载。')
          const blob = await response.blob()
          if (signal.aborted) return
          const url = URL.createObjectURL(blob)
          const link = document.createElement('a')
          link.href = url
          link.download = filename
          document.body.append(link)
          link.click()
          link.remove()
          window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
          setNotice('Top 原始日志已触发浏览器下载。')
        } else {
          const body = action === 'start'
            ? { confirm: true, serial, interval: topInterval, count: topCount, title: topTitle }
            : action === 'import' ? { confirm: true, capture_id: captureId } : { confirm: true, serial, capture_id: captureId }
          const init = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal }
          if (action === 'import' || action === 'pull') {
            let archive = topStatus?.archive
            let importId = captureId
            if (action === 'pull') {
              // 后端先确认安全停止；失败时立即退出，绝不继续导入。
              const next = await api<TopStatus>('/api/top/pull', init)
              if (signal.aborted) return
              setTopStatus(next)
              setTopPollError('')
              archive = next.archive
              importId = next.id
              setBusy('top-analyze')
            }
            let result: { id: string; duplicate: boolean }
            try {
              result = await api<{ id: string; duplicate: boolean }>('/api/top/import', {
                ...init, body: JSON.stringify({ confirm: true, capture_id: importId }),
              })
            } catch (failure) {
              throw new Error(`导入分析失败：${errorMessage(failure)}；本地归档已保留${archive ? `（${archive}）` : ''}，可在“更多操作”中重试本地导入或下载。`)
            }
            if (signal.aborted) return
            setNotice(`${result.duplicate ? '已打开已有 Top 分析会话' : action === 'pull' ? '日志拉取并分析完成' : '本地日志导入完成'}；本地归档已保留${archive ? `：${archive}` : '。'}`)
            onImported(result.id)
          } else {
            const next = await api<TopStatus>(`/api/top/${action}`, init)
            if (signal.aborted) return
            setTopStatus(next)
            setTopPollError('')
            setNotice(action === 'clear' ? '设备上的 Top 日志已清空；SysMonitor 日志和本地归档保留，本地归档仍可导入或下载。' : action === 'start' ? '设备端 Top 任务已启动，断连或退出本地服务不会停止。可随时使用“拉取日志并分析”，自动安全停止并保存日志。' : '已确认停止 Top 采集，已完成样本保留，可拉取日志并分析。')
          }
        }
      } finally {
        running.current = false
        if (!signal.aborted) { setBusy(''); setRevision((value) => value + 1) }
      }
    })
  }

  return <>
    <section hidden={view !== 'advanced'} className="panel config-panel" aria-label="高级采集设置">
      <div className="section-heading"><div><h2>采集参数</h2><p>参数用于下一次开始采集，修改后不会立即写入设备。</p></div></div>
      <div className="device-controls">
        <label>采样间隔（秒）<input type="number" min={1} max={3600} step={1} value={interval} disabled={locked} onChange={(event) => setInterval(Number(event.target.value))} /></label>
        <label className="device-check"><input type="checkbox" checked={tologcat} disabled={locked} onChange={(event) => setTologcat(event.target.checked)} />同时写 logcat</label>
<label className="device-check"><input type="checkbox" checked={asyncWrite} disabled={locked} onChange={(event) => setAsyncWrite(event.target.checked)} />异步写入（启动时生效）</label>
      </div>
      {(!Number.isInteger(interval) || interval < 1 || interval > 3600) && <p className="error" role="alert">采样间隔必须为 1–3600 秒的整数。</p>}
      <p className="muted">始终写入文件；采集属性为设备全局属性。异步模式仅对新启动实例保证生效。关闭页面或断连不会停止采集；参数仅在当前页面保留。</p>
    </section>
    <section hidden={view !== 'capture'} className="panel device-panel" aria-label="SysMonitor采集">
    <div className="device-heading"><div><h2>SysMonitor采集</h2><p className="muted">Android ARM64 · root adbd 或 su 0 · 配置、启停、停止后拉取分析</p></div>
      <button disabled={locked || devices.loading} onClick={() => { setStatus(null); setRevision((value) => value + 1) }}>刷新设备</button></div>
    {devices.error && <p className="error" role="alert">{devices.error}</p>}
    {disconnected && <p className="error" role="alert">设备已断开连接或不可用：{serial}（{selectedDevice?.state || '未连接'}）。请检查连接及 USB 调试授权；正在自动检测重连。</p>}
    {pollError && connected && <p className="error" role="alert">状态刷新失败：{pollError}</p>}
    {!devices.loading && devices.data?.length === 0 && <p className="muted">未发现设备。请连接 USB 并授权调试，或先在本机配置 ADB 网络连接。</p>}
    <div className="device-controls">
      <label>设备<select value={serial} disabled={locked} onChange={(event) => { setSelected(event.target.value); setStatus(null); setTopStatus(null); setTopPollError(''); mutation.setError(''); setNotice(''); setPollError('') }}>
        <option value="">请选择设备</option>{serial && !selectedDevice && <option value={serial}>{serial} · 已断开</option>}{devices.data.map((device) => <option key={device.serial} value={device.serial} disabled={device.state !== 'device'}>{device.model || device.serial} · {device.serial} · {device.state}</option>)}
      </select></label>
      <div className="capture-settings-summary"><span>下一次采集：{interval} 秒 · logcat {tologcat ? '开' : '关'} · 异步写入 {asyncWrite ? '开' : '关'}</span><button onClick={onAdvanced}>高级采集设置</button></div>
    </div>
    <div className="actions device-actions">
      <button disabled={locked || !connected} onClick={() => void perform('status')}>查询状态</button>
      <button className="primary" disabled={locked || !connected || !Number.isInteger(interval) || interval < 1 || interval > 3600} onClick={() => void perform('start')}>开始采集</button>
      <button disabled={locked || !connected} onClick={() => void perform('stop')}>停止采集</button>
      <button disabled={locked || !connected} onClick={() => void perform('pull')}>拉取日志并分析</button>
      <button className="danger" disabled={locked || !connected} onClick={() => void perform('delete-logs')}>删除车机性能日志</button>
    </div>
    <p className="muted">未部署时自动部署，已部署直接开始采集；连接后每 5 秒自动刷新状态。始终写入文件；不自动提权重启、不自动删除设备日志；手动删除需确认，并先停止采集。采集属性为设备全局属性；异步模式仅对新启动实例保证生效。拉取日志会自动先停止尚未结束的采集，停止失败则不拉取。历史日志会一并拉取，关闭页面或断连不会停止采集。</p>
    {feedbackSource === 'sysmonitor' && <>
      {busy && <p role="status">{busyMessage}</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="device-notice" role="status">{notice}</p>}
    </>}
    {current && <div className="device-status"><p>采集程序：{current.deployed ? '已部署' : '未部署（开始采集时自动部署）'} · 测试进程：{current.pids.length ? current.pids.join(', ') : '未运行'} · 权限：{current.privilege}</p>
      <p>采集开关：{current.properties.test || '默认关闭'} · 间隔：{current.properties.interval || '1'} 秒 · 文件：{current.properties.tofile || '1'} · logcat：{current.properties.tologcat || '1'} · 异步属性：{current.properties.async || '0'}</p>
      <details><summary>设备日志文件</summary><pre>{current.files || '暂无日志文件'}</pre></details></div>}
    <section aria-label="独立 Top 采集">
      <div className="section-heading"><div><h2>Top 采集</h2><p className="muted">自动推送脚本到设备独立运行，无需 sysmonitor 或 root；使用上方所选 ADB 设备。</p></div></div>
      <details>
        <summary>高级采集设置</summary>
        <div className="device-controls">
          <label>采样间隔（秒）<input type="number" min={1} max={3600} step="any" value={topInterval} disabled={locked || topActive} onChange={(event) => setTopInterval(Number(event.target.value))} /></label>
          <label>采样次数（-1 持续）<input type="number" min={-1} max={100000} step={1} value={topCount} disabled={locked || topActive} onChange={(event) => setTopCount(Number(event.target.value))} /></label>
          <label>会话名称<input value={topTitle} maxLength={120} placeholder="选填" disabled={locked || topActive} onChange={(event) => setTopTitle(event.target.value)} /></label>
        </div>
      </details>
      {!validTopSettings && <p className="error" role="alert">采样间隔须为 1–3600 秒；次数须为 -1 或 1–100000 的整数，请在高级采集设置中修改。</p>}
      <div className="actions device-actions">
        <button disabled={locked || !topAllowed.status} onClick={() => void performTop('status')}>查询状态</button>
        <button className="primary" disabled={locked || !topAllowed.start} onClick={() => void performTop('start')}>开始采集</button>
        <button disabled={locked || !topAllowed.stop} onClick={() => void performTop('stop')}>停止采集</button>
        <button disabled={locked || !topAllowed.pull} onClick={() => void performTop('pull')}>拉取日志并分析</button>
        <button className="danger" disabled={locked || !topAllowed.clear} onClick={() => void performTop('clear')}>清理设备日志</button>
      </div>
      <details>
        <summary>更多操作</summary>
        <div className="actions device-actions">
          <button disabled={locked || !topAllowed.import} onClick={() => void performTop('import')}>导入本地归档并分析</button>
          <button disabled={locked || !topAllowed.archive} onClick={() => void performTop('archive')}>下载原始日志</button>
        </div>
        <p className="muted">仅使用已经拉取的本地归档，无需设备在线；导入失败可在这里重试。</p>
      </details>
      <p className="muted">Top 参数与 SysMonitor 互不影响；每 2 秒尝试刷新所选设备状态。断连、关闭页面或退出本地服务不影响设备采集，重连后可查询、停止和拉取。单个原始文件最多 1GB（1,000,000,000 字节），达到上限前停止并保留完整样本。拉取日志并分析会先安全停止尚未结束的采集，停止失败则不拉取；拉取成功后保留本地归档并导入分析，成功后切换会话，不删除设备日志。</p>
      {feedbackSource === 'top' && <>
        {busy && <p role="status">{busyMessage}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="device-notice" role="status">{notice}</p>}
      </>}
      {topPollError && <p className="error" role="alert">Top 状态刷新失败：{topPollError}，正在重试。</p>}
      {!topStatus && !topPollError && <p role="status">{serial ? '正在查询 Top 状态…' : '请选择设备以查询 Top 任务。'}</p>}
      {topStatus && <div className="device-status" role="status">
        <p>Top 状态：{topLabels[topStatus.status] || topStatus.status} · 设备：{topStatus.serial || '—'} · 已采样：{topStatus.count} / {topStatus.target_count === -1 ? '持续' : topStatus.target_count} · 间隔：{topStatus.interval} 秒</p>
        <p>开始时间：{topStatus.started_at ? new Date(topStatus.started_at * 1000).toLocaleString('zh-CN', { hour12: false }) : topStatus.id ? '未记录（旧版任务）' : '—'} · 已采集时长：{topStatus.id ? formatCaptureDuration(topStatus.elapsed_seconds) : '—'}{(!connected || !topStatus.connected || topPollError) && topStatus.id ? '（最后已知）' : ''}</p>
        <p>{topStatus.remote_cleaned ? '设备日志已清理 · 清理前大小' : '原始文件'}：{(topStatus.bytes / 1_000_000).toFixed(2)} / {(topStatus.max_bytes / 1_000_000).toFixed(0)} MB · {topStatus.archive ? '已拉取到本地' : topStatus.remote_cleaned ? '无本地归档' : '尚未拉取到本地'}</p>
        {topStatus.warning && <p className="banner warning">{topStatus.warning}</p>}
        {topStatus.remote_path && <p>设备路径：{topStatus.remote_path}</p>}
        {(!connected || !topStatus.connected || topPollError) && <p className="error">当前状态未确认，仅展示最后已知数据；不代表设备采集已停止。同设备在线时仍可重试停止或拉取日志并分析，由后端确认任务身份与停止结果。{topStatus.connection_error}</p>}
        {topStatus.error && <p className="error">{topErrors[topStatus.error] || topStatus.error}{topStatus.archive ? '；已拉取样本可下载或导入。' : !topStatus.remote_cleaned && topStatus.bytes ? '；可拉取日志并分析，自动确认停止并保留已完成样本。' :''}</p>}
      </div>}
    </section>
  </section>
  {view !== 'capture' && (topActive || topPollError || topStatus?.error) && <div className="banner warning" role="status">Top 采集：{topPollError || topStatus?.error || `${topLabels[topStatus!.status] || topStatus!.status} · ${topStatus!.count} 次`}。请到数据集查看或停止任务。</div>}
  {view !== 'capture' && (disconnected || devices.error || pollError || error) && <div className="banner warning" role="status">设备采集提示：{disconnected ? '设备已断开连接或不可用，正在自动检测重连。' : devices.error || pollError || error} 请到数据采集查看详情。</div>}
  {view !== 'capture' && topStatus?.warning && <div className="banner warning" role="status">{topStatus.warning}</div>}
  {view !== 'capture' && notice && <div className="banner success device-notice" role="status">{notice}</div>}
  {view !== 'capture' && busy && <div className="banner info" role="status">{busyMessage}</div>}
  </>
}