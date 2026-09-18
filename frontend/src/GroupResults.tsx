import { useState } from 'react'
import { formatNumber, formatStatistic, formatTime, groupPath, useResource } from './api'
import type { GroupAnalysis, GroupResources, ProcessGroup } from './api'
import { SeriesChart } from './Chart'

function percent(value: number, cycles: number) { return cycles ? `${formatNumber(value / cycles * 100, 2)} %` : '—' }

export function GroupResults({ id, groups, selected, onSelect, revision, disabled }: {
  id: string; groups: ProcessGroup[]; selected: string; onSelect: (gid: string) => void; revision: number; disabled: boolean
}) {
  return <>
    <div className="section-heading"><h2>进程组分析</h2><span>仅分析已保存配置 · 不随排行时间段改变</span></div>
    <section className="panel config-panel">
      <label>选择分析组 <select value={selected} disabled={disabled} onChange={(event) => onSelect(event.target.value)}>
        <option value="">请选择已保存组</option>{selected && !groups.some((group) => group.id === selected) && <option value={selected}>当前已选组</option>}
        {groups.map((group) => <option key={group.id} value={group.id}>{group.name} · 段 {group.segment}</option>)}
      </select></label>
      <p className="muted">先计算每周期完整成员合计，再计算均值、最大值及最近秩 P95 / P99；绝不相加成员峰值或分位数。不合计 wait 和分类字段。</p>
      <p className="banner warning">DP 为低频采样，缓冲区可能跨进程共享、重复归属，进程组 DP 合计不等于系统 D 总量，也不能与 RSS 简单相加。</p>
    </section>
    {selected && <div key={`${id}:${selected}`}>
      <ResourceResults id={id} gid={selected} revision={revision} disabled={disabled} />
      <KindResults id={id} gid={selected} kind="P" revision={revision} disabled={disabled} />
      <KindResults id={id} gid={selected} kind="DP" revision={revision} disabled={disabled} />
    </div>}
  </>
}

function ResourceResults({ id, gid, revision, disabled }: { id: string; gid: string; revision: number; disabled: boolean }) {
  const [retry, setRetry] = useState(0)
  const { data, loading, error } = useResource<GroupResources>(`${groupPath(id, gid)}/resources`, revision + retry)
  return <section className="panel statistics-panel">
    <div className="panel-heading"><h3>场景成员资源实测</h3><button disabled={disabled || loading} onClick={() => setRetry((v) => v + 1)}>刷新 / 重试</button></div>
    {loading && <p role="status">正在统计成员原始样本…</p>}
    {error && <p className="banner error" role="alert">{error}</p>}
    {data && <>
      <p>{data.group.name} · 实测场景：{data.group.scene} · 段 {data.group.segment} · 闭区间 {data.group.start === null ? '不限' : formatTime(data.group.start)} → {data.group.end === null ? '不限' : formatTime(data.group.end)}</p>
      <p className="muted">平台：{data.settings.cpu_platform || '未配置'} · 单核 KDMIPS 系数：{formatNumber(data.settings.kdmips_per_core)}。K = 单核 CPU 百分比 ÷ 100 × 系数。前后台需求 Y/N 不代表实测场景；未标注的场景不推断归属，未采集场景显示 —。</p>
      <div className="table-scroll"><table><thead><tr><th rowSpan={2}>模块 / 用途</th><th rowSpan={2}>进程 / PID</th><th rowSpan={2}>服务分类 / 关联服务</th><th rowSpan={2}>前 / 后台需求</th>{['前台', '后台'].map((scene) => <th key={scene} colSpan={9}>{scene}实测</th>)}</tr><tr>{['前台', '后台'].flatMap((scene) => ['CPU 样本', 'CPU P95 %', 'P95 K', 'CPU 峰值 %', '峰值 K', 'RSS 峰值', '读取峰值 / 周期', '写入峰值 / 周期', '累计读 / 写'].map((label) => <th key={`${scene}:${label}`}>{label}</th>))}</tr></thead><tbody>
        {data.rows.map(({ member, statistics: s, kdmips: k }) => <tr key={JSON.stringify([member.pid, member.name])}>
          <td className="wrap-text">{data.group.name}<small>{member.purpose || '待填写'}</small><small>{member.process_type || '待填写'}</small></td><td className="wrap-text">{member.name}<small>PID {member.pid}</small></td><td className="wrap-text">{member.service_category}<small>{member.related_service || '待填写'}</small></td><td>{member.foreground} / {member.background}</td>
          {['前台', '后台'].map((scene) => <ResourceCells key={scene} available={data.group.scene === scene} s={s} k={k} />)}
        </tr>)}
      </tbody></table></div>
      {data.group.scene === '未标注' && <p className="banner warning">已保存组未标注场景；下面保留未归属的成员实测，不能填入前台或后台列。</p>}
      {data.group.scene === '未标注' && <div className="table-scroll"><table><thead><tr><th>进程 / PID</th><th>CPU 样本</th><th>CPU P95 %</th><th>P95 K</th><th>CPU 峰值 %</th><th>峰值 K</th><th>RSS 峰值</th><th>读取峰值 / 周期</th><th>写入峰值 / 周期</th><th>累计读 / 写</th></tr></thead><tbody>{data.rows.map((row) => <tr key={JSON.stringify([row.member.pid, row.member.name])}><td>{row.member.name} / {row.member.pid}</td><ResourceCells available s={row.statistics} k={row.kdmips} /></tr>)}</tbody></table></div>}
      <p className="muted">{data.note} 成员独立统计不要求其他成员齐全；不可相加成员峰值或分位数。下方另列严格完整周期组总量。RSS 与 dmabuf 不相加。</p>
    </>}
  </section>
}

function ResourceCells({ available, s, k }: { available: boolean; s: GroupResources['rows'][number]['statistics']; k: GroupResources['rows'][number]['kdmips'] }) {
  const values = [formatNumber(s.cpu1c.count, 0), formatNumber(s.cpu1c.p95), formatNumber(k.p95), formatNumber(s.cpu1c.max), formatNumber(k.max), formatStatistic(s.rss_kb.max, 'KB'), formatStatistic(s.rd_kb.max, 'KB'), formatStatistic(s.wr_kb.max, 'KB'), `${formatStatistic(s.rd_kb.total, 'KB')} / ${formatStatistic(s.wr_kb.total, 'KB')}`]
  return <>{values.map((value, index) => <td key={index}>{available ? value : '—'}</td>)}</>
}

function KindResults({ id, gid, kind, revision, disabled }: { id: string; gid: string; kind: 'P' | 'DP'; revision: number; disabled: boolean }) {
  const [retry, setRetry] = useState(0)
  const [field, setField] = useState('')
  const { data, loading, error } = useResource<GroupAnalysis>(`${groupPath(id, gid)}/analysis?kind=${kind}&limit=600`, revision + retry)
  const entries = Object.entries(data?.statistics.metrics ?? {})
  const selected = entries.find(([key]) => key === field) ?? entries[0]
  return <section className="panel statistics-panel group-results">
    <div className="panel-heading"><h3>{kind} · 进程组周期合计</h3><button disabled={disabled || loading} onClick={() => setRetry((v) => v + 1)}>刷新 / 重试</button></div>
    {loading && <p role="status">正在计算全部原始周期统计及趋势…</p>}
    {error && <p className="banner error" role="alert">{error}，可点击上方重试。</p>}
    {data && <>
      <p>{data.group.name} · 段 {data.group.segment} · {data.group.members.length} 个成员 · {data.group.scene} · 实际周期范围 {formatTime(data.statistics.start)} → {formatTime(data.statistics.end)}</p>
      <div className="table-scroll"><table><thead><tr><th>覆盖范围</th><th>全部周期</th><th>完整周期</th><th>部分周期</th><th>缺失周期</th><th>重复周期</th></tr></thead><tbody><tr>
        <td>{kind}</td><td>{formatNumber(data.coverage.cycles, 0)}</td>
        {(['complete', 'partial', 'missing', 'duplicates'] as const).map((key) => <td key={key}>{formatNumber(data.coverage[key], 0)}<small>{percent(data.coverage[key], data.coverage.cycles)}</small></td>)}
      </tr></tbody></table></div>
      <p className="muted">分母为所选段及闭区间内全部记录周期。完整、部分、缺失三类互斥；重复是部分周期中的异常标记，不能再相加。完整记录不保证每项指标有效；无周期时覆盖率为 —。</p>
      <p className="muted">{data.statistics.method}</p>
      <div className="table-scroll"><table><thead><tr><th>指标 / 原始字段</th><th>有效周期</th><th>指标覆盖率</th><th>最小</th><th>均值</th><th>P95</th><th>P99</th><th>最大</th><th>增量累计</th></tr></thead><tbody>
        {entries.map(([key, item]) => <tr key={key}><td><strong>{item.label}</strong><small>{key}</small><small>{item.note}</small></td>
          <td>{formatNumber(item.count, 0)}</td><td>{percent(item.count, data.coverage.cycles)}</td>
          {(['min', 'avg', 'p95', 'p99', 'max'] as const).map((stat) => <td key={stat}>{formatStatistic(item[stat], item.unit)}</td>)}
          <td>{formatStatistic(item.total, item.unit, true)}</td></tr>)}
      </tbody></table></div>
      <label className="metric-select">全部数值指标趋势 <select disabled={disabled || !entries.length} value={selected?.[0] ?? ''} onChange={(event) => setField(event.target.value)}>
        {!entries.length && <option value="">无数值指标</option>}{entries.map(([key, item]) => <option key={key} value={key}>{item.label} · {key}</option>)}
      </select></label>
      {selected && <SeriesChart title={`${kind} · ${selected[1].label}`} unit={selected[1].unit} metrics={[{ field: selected[0], label: selected[1].label }]} data={data.series} mode="scatter" note="趋势与统计复用同一组合响应；切换指标不重新请求。统计使用全部有效原始周期，不从采样点推算。I/O 是周期增量，不再次差分、不换算速率；缺失不补零。" />}
      <p className="muted">{data.note}</p>
    </>}
  </section>
}