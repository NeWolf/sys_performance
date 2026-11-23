import { useState } from 'react'
import { formatBytes, formatTime, storageMutation, useResource } from './api'
import type { Session, StorageCleanupResult, StorageDeleteResult, StorageReclaimResult, StorageUsage } from './api'
import { useEditGuard } from './Editing'
import './Storage.css'

type Props = {
  sessions: Session[]
  perform: (label: string, action: (signal: AbortSignal) => Promise<void>) => Promise<void>
  onDeleted: (ids: string[]) => void
  onNotice: (message: string) => void
}
type Sort = 'newest' | 'oldest' | 'largest' | 'smallest'

export const storageDeleteWarning = '分析数据删除后不可撤销，并同步清理不再被其他会话引用的受管日志。仍被共享引用的日志、外部日志及未关联的历史日志不会误删。数据库文件不会立即缩小，需要另行执行“回收数据库空间”。'

export function Storage({ sessions, perform, onDeleted, onNotice }: Props) {
  const guard = useEditGuard()
  const [revision, setRevision] = useState(0)
  const usage = useResource<StorageUsage>('/api/storage', revision)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [sort, setSort] = useState<Sort>('newest')
  const [warnings, setWarnings] = useState<string[]>([])
  const data = usage.data
  const metadata = new Map(sessions.map((session) => [session.id, session]))
  const timestamp = (id: string) => {
    const created = metadata.get(id)?.created
    const value = typeof created === 'string' ? Date.parse(created) : created
    return value != null && Number.isFinite(value) ? value : 0
  }
  const rows = [...(data?.sessions ?? [])].sort((a, b) => {
    const difference = sort === 'largest' || sort === 'smallest'
      ? a.total_bytes - b.total_bytes : timestamp(a.id) - timestamp(b.id)
    return (sort === 'largest' || sort === 'newest' ? -difference : difference) || a.id.localeCompare(b.id)
  })
  const chosen = rows.filter((row) => selected.has(row.id))
  const locked = guard.busy || usage.loading
  const refresh = () => setRevision((value) => value + 1)

  function deleteSelected() {
    if (!chosen.length || chosen.length > 1000 || locked || !guard.confirm() || !window.confirm(`确定删除所选 ${chosen.length} 个会话？\n\n${storageDeleteWarning}`)) return
    const ids = chosen.map((row) => row.id)
    void perform('正在删除所选会话并清理受管日志…', async (signal) => {
      setWarnings([])
      try {
        const result = await storageMutation<StorageDeleteResult>('/api/storage/delete', signal, ids)
        if (signal.aborted) return
        const removed = [...result.deleted_ids, ...result.missing_ids]
        onDeleted(removed)
        setSelected((previous) => new Set([...previous].filter((id) => !removed.includes(id))))
        setWarnings(result.warnings)
        onNotice(`已删除 ${result.deleted_ids.length} 个会话${result.missing_ids.length ? `，另有 ${result.missing_ids.length} 个会话已不存在` : ''}；实际释放日志 ${formatBytes(result.logs_freed_bytes)}。数据库空间需另行回收。`)
      } finally {
        if (!signal.aborted) refresh()
      }
    })
  }

  function maintain(action: 'retry-cleanup' | 'reclaim') {
    const reclaim = action === 'reclaim'
    const message = reclaim
      ? '回收数据库空间可能耗时很久，期间请勿关闭页面或程序，也不要执行导入、分析或其他写入。服务端将先进行磁盘空间预检，空间不足或数据库繁忙时会拒绝执行。是否继续？'
      : '重试清理将永久删除已进入待清理队列且不再被引用的受管日志，不会删除外部或未关联历史日志。操作可能耗时很久，请勿关闭页面或程序；请留意磁盘空间，后续数据库回收会另行预检。是否继续？'
    if (locked || !guard.confirm('有未保存的配置；维护不会保存这些草稿，操作期间编辑将锁定。是否继续？') || !window.confirm(message)) return
    void perform(reclaim ? '正在预检磁盘并回收数据库空间，可能需要较长时间…' : '正在重试清理受管日志…', async (signal) => {
      setWarnings([])
      try {
        if (reclaim) {
          const result = await storageMutation<StorageReclaimResult>('/api/storage/reclaim', signal)
          if (signal.aborted) return
          onNotice(`${result.skipped ? '无需回收，已跳过' : '数据库回收完成'}：${formatBytes(result.before_bytes)} → ${formatBytes(result.after_bytes)}，实际释放 ${formatBytes(result.reclaimed_bytes)}。`)
        } else {
          const result = await storageMutation<StorageCleanupResult>('/api/storage/retry-cleanup', signal)
          if (signal.aborted) return
          setWarnings(result.warnings)
          onNotice(`清理重试完成，实际释放日志 ${formatBytes(result.logs_freed_bytes)}。`)
        }
      } finally {
        if (!signal.aborted) refresh()
      }
    })
  }

  return <section className="storage-view" aria-label="存储管理">
    <div className="storage-toolbar">
      <p className="muted">仅进入此页、手动刷新或操作后读取统计，不自动轮询。</p>
      <button disabled={locked} onClick={refresh}>{usage.error ? '重试读取统计' : '刷新统计'}</button>
    </div>
    {usage.loading && <p className="banner info" role="status">正在读取存储统计…</p>}
    {usage.error && <div className="banner error" role="alert">{usage.error}<p>若服务繁忙（409），请等待导入、分析或维护结束后手动重试。</p></div>}
    {warnings.length > 0 && <div className="banner warning" role="status"><strong>操作提示</strong><ul>{warnings.map((message, index) => <li key={index}>{message}</li>)}</ul></div>}
    {data && <>
      <div className="stats-grid storage-stats">
        <article className="panel stat"><p>总占用</p><div><strong>{formatBytes(data.total_bytes)}</strong></div><small>以服务端统计口径为准</small></article>
        <article className="panel stat"><p>数据库文件</p><div><strong>{formatBytes(data.database_bytes)}</strong></div><small>WAL {formatBytes(data.wal_bytes)} · SHM {formatBytes(data.shm_bytes)}</small></article>
        <article className="panel stat"><p>受管日志</p><div><strong>{formatBytes(data.logs_bytes)}</strong></div><small>待清理 {data.pending_cleanup_count} 项</small></article>
        <article className="panel stat"><p>磁盘可用</p><div><strong>{formatBytes(data.free_bytes)}</strong></div><small>数据库可回收 {formatBytes(data.reclaimable_bytes)}</small></article>
      </div>
      <section className="panel storage-sessions">
        <div className="storage-toolbar">
          <div><h2>会话占用</h2><p className="muted">共 {rows.length} 个 · 已选 {chosen.length} 个 · 合计约 {formatBytes(chosen.reduce((sum, row) => sum + row.total_bytes, 0))}</p></div>
          <label>排序 <select value={sort} disabled={locked} onChange={(event) => setSort(event.target.value as Sort)}><option value="newest">时间：新到旧</option><option value="oldest">时间：旧到新</option><option value="largest">大小：大到小</option><option value="smallest">大小：小到大</option></select></label>
        </div>
        <div className="storage-toolbar storage-selection">
          <div className="actions"><button disabled={locked || !rows.length} onClick={() => setSelected(new Set(rows.slice(0, 1000).map((row) => row.id)))}>{rows.length > 1000 ? '全选前 1000个' : '全选'}</button><button disabled={locked || !selected.size} onClick={() => setSelected(new Set())}>清空选择</button></div>
          <button className="danger" disabled={locked || !chosen.length} onClick={deleteSelected}>删除所选（{chosen.length}）</button>
        </div>
        <div className="table-scroll"><table className="storage-table">
          <thead><tr><th scope="col">选择</th><th scope="col">会话 / 创建时间</th><th scope="col">数据库估算</th><th scope="col">日志</th><th scope="col">合计</th></tr></thead>
          <tbody>{rows.map((row) => {
            const session = metadata.get(row.id)
            return <tr key={row.id} className={selected.has(row.id) ? 'selected-row' : ''}>
              <td><input type="checkbox" aria-label={`选择会话 ${session?.name ?? row.id}`} checked={selected.has(row.id)} disabled={locked || (!selected.has(row.id) && chosen.length >= 1000)} onChange={(event) => {
                const checked = event.target.checked
                if (checked && chosen.length >= 1000) return
                setSelected((previous) => { const next = new Set(previous); if (checked) next.add(row.id); else next.delete(row.id); return next })
              }} /></td>
              <td className="storage-name"><strong>{session?.name ?? row.id}</strong><small>{timestamp(row.id) ? formatTime(timestamp(row.id)) : '创建时间暂不可用'}</small>{row.unmanaged_logs && <small className="storage-unmanaged">外部 / 历史未关联日志不自动清理</small>}</td>
              <td>{row.estimated ? '≈ ' : ''}{formatBytes(row.database_bytes)}<small>含缓存 {formatBytes(row.cache_bytes)}</small></td>
              <td>{formatBytes(row.logs_bytes)}</td><td>{row.estimated ? '≈ ' : ''}{formatBytes(row.total_bytes)}</td>
            </tr>
          })}</tbody>
        </table></div>
        {!rows.length && <p className="loading-state">暂无可管理的会话。</p>}
        <div className="storage-note"><p>每次最多删除 1000 个会话，可按当前排序分批操作。会话数据库占用为估算，缓存已包含其中；共享日志可能出现在多个会话，所选合计不等于实际释放量。删除不会自动缩小数据库。</p><details><summary>统计方式与日志归属</summary><p>{data.estimate_method}</p>{data.unmanaged_logs && <p>存在无日志归属记录的会话，外部或未关联历史日志不会自动清理。</p>}</details></div>
      </section>
      {data.warnings.length > 0 && <div className="banner warning" role="status"><strong>统计提示</strong><ul>{data.warnings.map((message, index) => <li key={index}>{message}</li>)}</ul></div>}
      <section className="panel storage-maintenance">
        <div><h2>存储维护</h2><p className="muted">删除后仍有占用？先重试待清理日志，再单独回收数据库空闲页。维护可能耗时很久，回收前由服务端预检磁盘空间。</p></div>
        <div className="actions"><button disabled={locked || !data.pending_cleanup_count} onClick={() => maintain('retry-cleanup')}>重试清理日志（{data.pending_cleanup_count}）</button><button disabled={locked} onClick={() => maintain('reclaim')}>回收数据库空间</button></div>
      </section>
    </>}
  </section>
}