import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from './api'
import { useEditGuard, useMutation } from './Editing'

type Device = { serial: string; state: string; model: string }
type Status = { serial: string; pids: string[]; properties: Record<string, string>; files: string; privilege: string; deployed: boolean }
type PullResult = { id: string; duplicate: boolean; archive: string; files: string[] }

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
  const [status, setStatus] = useState<Status | null>(null)
  const running = useRef(false)
  const polling = useRef<Promise<void> | null>(null)
  const [pollError, setPollError] = useState('')
  const locked = Boolean(busy) || disabled
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
        if (running.current || polling.current) return
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

  async function perform(action: 'status' | 'start' | 'stop' | 'pull') {
    if (!serial || !connected || disabled || running.current) return
    if (action === 'pull' && !guard.confirm('拉取成功后将切换会话并放弃未保存配置，是否继续？')) return
    if (action === 'start' && !window.confirm(`将在 ${serial} 开始采集；若未部署，将先自动部署采集程序，已部署则直接使用。使用现有 root / su 权限，持久化属性会影响系统 sysmonitor，关闭页面不会停止采集。确认继续？`)) return
    if (action === 'stop' && !window.confirm('将关闭设备共享采集开关，并终止该路径的测试进程。确认停止？')) return
    await mutation.run(async (signal) => {
      running.current = true
      setBusy(action)
      setNotice('')
      setPollError('')
      try {
        // 等待已发出的状态查询结束，避免与后端设备操作锁冲突。
        await polling.current
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
            setNotice(action === 'start' ? '采集已开始，状态每 5 秒自动刷新；不会自动清空旧日志。' : '采集开关已关闭，测试进程已退出。')
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
    <section hidden={view !== 'capture'} className="panel device-panel" aria-label="ADB 设备采集">
    <div className="device-heading"><div><h2>ADB 设备采集</h2><p className="muted">Android ARM64 · root adbd 或 su 0 · 配置、启停、停止后拉取分析</p></div>
      <button disabled={locked || devices.loading} onClick={() => { setStatus(null); setRevision((value) => value + 1) }}>刷新设备</button></div>
    {devices.error && <p className="error" role="alert">{devices.error}</p>}
    {disconnected && <p className="error" role="alert">设备已断开连接或不可用：{serial}（{selectedDevice?.state || '未连接'}）。请检查连接及 USB 调试授权；正在自动检测重连。</p>}
    {pollError && connected && <p className="error" role="alert">状态刷新失败：{pollError}</p>}
    {!devices.loading && devices.data?.length === 0 && <p className="muted">未发现设备。请连接 USB 并授权调试，或先在本机配置 ADB 网络连接。</p>}
    <div className="device-controls">
      <label>设备<select value={serial} disabled={locked} onChange={(event) => { setSelected(event.target.value); setStatus(null); mutation.setError(''); setNotice(''); setPollError('') }}>
        <option value="">请选择设备</option>{serial && !selectedDevice && <option value={serial}>{serial} · 已断开</option>}{devices.data.map((device) => <option key={device.serial} value={device.serial} disabled={device.state !== 'device'}>{device.model || device.serial} · {device.serial} · {device.state}</option>)}
      </select></label>
      <div className="capture-settings-summary"><span>下一次采集：{interval} 秒 · logcat {tologcat ? '开' : '关'} · 异步写入 {asyncWrite ? '开' : '关'}</span><button onClick={onAdvanced}>高级采集设置</button></div>
    </div>
    <div className="actions device-actions">
      <button disabled={locked || !connected} onClick={() => void perform('status')}>查询状态</button>
      <button className="primary" disabled={locked || !connected || !Number.isInteger(interval) || interval < 1 || interval > 3600} onClick={() => void perform('start')}>开始采集</button>
      <button disabled={locked || !connected} onClick={() => void perform('stop')}>停止采集</button>
      <button disabled={locked || !connected} onClick={() => void perform('pull')}>拉取日志并分析</button>
    </div>
    <p className="muted">未部署时自动部署，已部署直接开始采集；连接后每 5 秒自动刷新状态。始终写入文件；不自动提权重启、不删除设备日志。采集属性为设备全局属性；异步模式仅对新启动实例保证生效。历史日志会一并拉取，关闭页面或断连不会停止采集。</p>
    {busy && <p role="status">正在执行设备操作，请勿关闭页面…</p>}
    {error && <p className="error" role="alert">{error}</p>}
    {notice && <p className="device-notice" role="status">{notice}</p>}
    {current && <div className="device-status"><p>采集程序：{current.deployed ? '已部署' : '未部署（开始采集时自动部署）'} · 测试进程：{current.pids.length ? current.pids.join(', ') : '未运行'} · 权限：{current.privilege}</p>
      <p>采集开关：{current.properties.test || '默认关闭'} · 间隔：{current.properties.interval || '1'} 秒 · 文件：{current.properties.tofile || '1'} · logcat：{current.properties.tologcat || '1'} · 异步属性：{current.properties.async || '0'}</p>
      <details><summary>设备日志文件</summary><pre>{current.files || '暂无日志文件'}</pre></details></div>}
  </section>
  {view !== 'capture' && (disconnected || devices.error || pollError || error) && <div className="banner warning" role="status">设备采集提示：{disconnected ? '设备已断开连接或不可用，正在自动检测重连。' : devices.error || pollError || error} 请到数据采集查看详情。</div>}
  {view !== 'capture' && notice && <div className="banner success device-notice" role="status">{notice}</div>}
  {view !== 'capture' && busy && <div className="banner info" role="status">正在执行设备操作，请勿关闭页面…</div>}
  </>
}