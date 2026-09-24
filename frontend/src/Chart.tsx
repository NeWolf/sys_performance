import { useEffect, useRef } from 'react'
import { init, use as registerCharts } from 'echarts/core'
import { LineChart, ScatterChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, MarkLineComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'
import type { SeriesData, StatisticsData } from './api'
import { formatMemory, formatTime, formatNumber, formatStatistic, useResource } from './api'

registerCharts([LineChart, ScatterChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, AriaComponent, MarkLineComponent, CanvasRenderer])

export interface Metric {
  field: string
  label: string
}
export interface ReferenceMetric extends Metric {
  data?: SeriesData
  scale?: number
}
interface ChartProps {
  title: string
  unit: string
  metrics: Metric[]
  references?: ReferenceMetric[]
  data?: SeriesData
  loading?: boolean
  error?: string
  mode?: 'line' | 'scatter' | 'bar'
  note?: string
  // 按原始字段匹配；数值必须来自同范围的原始统计，单位与趋势一致。
  percentiles?: Record<string, { p95?: number | null; p99?: number | null }>
  defaultShowPercentiles?: boolean
}

// 数值格式化与请求类型统一来自 API 层。

export function SeriesChart({ title, unit, metrics, references, data, loading, error, mode = 'line', note, percentiles, defaultShowPercentiles = false }: ChartProps) {
  const [showPercentiles, setShowPercentiles] = useState(defaultShowPercentiles)
  const hasPercentiles = metrics.some(({ field }) => [percentiles?.[field]?.p95, percentiles?.[field]?.p99].some((value) => typeof value === 'number' && Number.isFinite(value)))
  const host = useRef<HTMLDivElement>(null)
  const memoryUnit = unit === 'MB' ? 'MB' : unit.startsWith('KB') ? 'KB' : null
  const displayUnit = memoryUnit ? `容量（自动换算）${unit.includes('周期') ? ' / 周期' : ''}` : unit
  const hasData = Boolean(data?.points.some((point) => metrics.some(({ field }) => typeof point[field] === 'number' && Number.isFinite(point[field])))
    || references?.some(({ data: source, field }) => source?.points.some((point) => typeof point[field] === 'number' && Number.isFinite(point[field]))))
  useEffect(() => {
    if (!host.current || !hasData || loading || error) return
    const element = host.current
    let chart: ReturnType<typeof init> | undefined
    const option: EChartsOption = {
      animation: false,
      color: ['#4f6cf6', '#11a897', '#ed9b3b', '#b178e8', '#e16c87'],
      textStyle: { fontFamily: 'system-ui, sans-serif', color: '#62718a' },
      aria: { enabled: true, description: `${title}，${displayUnit}，横轴为本地时间。` },
      tooltip: {
        trigger: mode === 'scatter' ? 'item' : 'axis', renderMode: 'richText', confine: true,
        formatter: (params) => {
          const items = Array.isArray(params) ? params : [params]
          const first = items[0]?.value
          const timestamp = Array.isArray(first) && typeof first[0] === 'number' ? first[0] : null
          return [formatTime(timestamp), ...items.map((item) => {
            const raw = Array.isArray(item.value) ? item.value[1] : null
            const value = typeof raw === 'number' ? raw : null
            const text = memoryUnit ? formatMemory(value, memoryUnit) : formatNumber(value)
            const suffix = memoryUnit ? (unit.includes('周期') ? ' / 周期' : '') : ` ${unit}`
            return `${item.seriesName}: ${text}${value == null ? '' : suffix}`
          })].join('\n')
        },
      },
      legend: { top: 0, type: 'scroll', textStyle: { color: '#62718a', fontSize: 11 } },
      grid: { top: 50, left: 88, right: 28, bottom: 94 },
      xAxis: {
        type: 'time', min: 'dataMin', max: 'dataMax', name: '本地时间', nameLocation: 'middle', nameGap: 47,
        axisLabel: { hideOverlap: true, formatter: (value: number) => formatTime(value, 'axis') },
        axisPointer: { label: { formatter: (params) => formatTime(Number(params.value)) } },
        splitLine: { show: false }, axisLine: { lineStyle: { color: '#d9e1ec' } },
      },
      yAxis: {
        type: 'value', name: memoryUnit ? (unit.includes('周期') ? '容量 / 周期' : '容量') : unit,
        axisLabel: { formatter: (value: number) => memoryUnit ? formatMemory(value, memoryUnit) : formatNumber(value) },
        splitLine: { lineStyle: { color: '#edf1f6', type: 'dashed' } },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 2, borderColor: 'transparent', labelFormatter: (value: number) => formatTime(value) }],
      series: [
        ...metrics.map((metric) => ({ ...metric, data, scale: 1, reference: false })),
        ...(references ?? []).map((metric) => ({ ...metric, reference: true })),
      ].map(({ field, label, data: source, scale = 1, reference }) => {
        const seriesMode = reference ? 'line' : mode
        const values: (number | null)[][] = []
        source?.points.forEach((point, index) => {
          const previous = source.points[index - 1]
          // 各曲线保留自身时间与周期，不对独立降采样的数据按数组下标拼接。
          if (seriesMode === 'line' && previous && (point.segment !== previous.segment || point.pid !== previous.pid || point.cycle > previous.cycle + 1 || point.ts <= previous.ts)) {
            values.push([point.ts, null])
          }
          const value = point[field]
          values.push([point.ts, typeof value === 'number' && Number.isFinite(value) ? value * scale : null])
        })
        return {
          name: label, type: seriesMode, data: values, connectNulls: false,
          showSymbol: true, symbolSize: seriesMode === 'scatter' ? 6 : 3,
          lineStyle: { width: reference ? 3 : 2, type: reference ? 'dashed' : 'solid' },
          z: reference ? 4 : 3, emphasis: { focus: 'series' },
          markLine: showPercentiles && !reference ? {
            silent: true, symbol: 'none',
            lineStyle: { type: 'dashed', width: 1 },
            label: { show: true, position: 'insideEndTop', formatter: '{b}' },
            data: (['p95', 'p99'] as const).flatMap((key) => {
              const value = percentiles?.[field]?.[key]
              return typeof value === 'number' && Number.isFinite(value)
                ? [{ name: `${label} ${key.toUpperCase()} · ${formatStatistic(value, unit)}`, yAxis: value }]
                : []
            }),
          } : undefined,
        }
      }),
    }
    // 隐藏分区可能收到请求结果，等待容器可见再初始化。
    const resize = () => {
      if (!element.clientWidth || !element.clientHeight) return
      if (!chart){
        chart = init(element)
        chart.setOption(option)
      } else chart.resize()
    }
    const observer = new ResizeObserver(resize)
    observer.observe(element)
    resize()
    return () => { observer.disconnect(); chart?.dispose() }
  }, [data, metrics, references, title, unit, mode, hasData, loading, error, memoryUnit, displayUnit, percentiles, showPercentiles])

  return (
    <section className="panel chart-panel" aria-label={title}>
      <div className="panel-heading"><h3>{title}</h3><span className="unit">{displayUnit}</span></div>
      {percentiles && <label><input type="checkbox" checked={showPercentiles} disabled={!hasPercentiles} onChange={(event) => setShowPercentiles(event.target.checked)} />显示 P95 / P99 参考线（原始样本分位数，非准入阈值）{!hasPercentiles && ' · 暂无有效分位数'}</label>}
      {loading ? <div className="chart-placeholder" role="status">正在读取趋势…</div>
        : error ? <div className="chart-placeholder error" role="alert">{error}</div>
        : !hasData ? <div className="chart-placeholder">此时间段暂无对应记录，不以零值填充</div>
        : <div ref={host} className="chart" role="img" aria-label={`${title}趋势，单位 ${displayUnit}`} />}
      <p className="chart-caption">
        {data && !error && !loading && `${data.sampled ? '已降采样 · 限额内优先保留极值' : '原始记录'} · 展示 ${formatNumber(data.points.length, 0)} / ${formatNumber(data.total, 0)} 条。`}
        {note || '拖动底部滑块缩放；缺失记录不补零。'}
      </p>
    </section>
  )
}

export interface ComparisonBarItem {
  name: string
  baseline: number | null
  target: number | null
  delta: number | null
  percent: number | null
  comparable: boolean
}

export function ComparisonBars({ title, unit, items, ranking = false, note, baselineLabel, targetLabel }: {
  title: string
  unit: string
  items: ComparisonBarItem[]
  ranking?: boolean
  note?: string
  baselineLabel: string
  targetLabel: string
}) {
  const host = useRef<HTMLDivElement>(null)
  const valid = (value: number | null) => value != null && Number.isFinite(value)
  const rows = ranking ? items.filter((item) => item.comparable && valid(item.delta)) : items
  const hasData = rows.some((item) => ranking ? valid(item.delta) : valid(item.baseline) || valid(item.target))
  const deltaUnit = unit === '%' ? '百分点' : unit
  const displayUnit = ranking ? deltaUnit : unit
  useEffect(() => {
    if (!host.current || !hasData) return
    const element = host.current
    let chart: ReturnType<typeof init> | undefined
    const text = (value: number | null, suffix: string, signed = false) => value == null || !Number.isFinite(value)
      ? '—（缺失）' : `${signed && value > 0 ? '+' : ''}${formatNumber(value, 2)} ${suffix}`
    const option: EChartsOption = {
      animation: false,
      color: ['#4f6cf6', '#11a897'],
      textStyle: { fontFamily: 'system-ui, sans-serif', color: '#62718a' },
      aria: { enabled: true, description: `${title}，单位 ${displayUnit}。缺失不补零，详细数值见下方表格。` },
      tooltip: {
        trigger: 'axis', renderMode: 'richText', confine: true,
        axisPointer: { type: 'shadow' },
        formatter: (params) => {
          const first = (Array.isArray(params) ? params : [params])[0]
          const item = first && rows[first.dataIndex]
          if (!item) return ''
          return [item.name, `${baselineLabel}：${text(item.baseline, unit)}`, `${targetLabel}：${text(item.target, unit)}`,
            item.comparable ? `差值：${text(item.delta, deltaUnit, true)}` : '口径不同，不计算差值',
            `相对变化：${item.percent == null ? `—（${baselineLabel}为零、缺失或不可比）` : text(item.percent, '%', true)}`].join('\n')
        },
      },
      legend: { show: !ranking, top: 0, data: ['baseline', 'target'], formatter: (name: string) => name === 'baseline' ? baselineLabel : targetLabel },
      grid: { top: 40, left: 180, right: 30, bottom: 48 },
      xAxis: {
        type: 'value', name: displayUnit, nameLocation: 'middle', nameGap: 30,
        axisLabel: { hideOverlap: true, formatter: (value: number) => formatNumber(value) },
        splitLine: { lineStyle: { color: '#edf1f6', type: 'dashed' } },
      },
      yAxis: {
        type: 'category', inverse: true, data: rows.map((item) => item.name),
        axisLabel: { interval: 0, width: 160, overflow: 'truncate' },
        axisTick: { show: false }, axisLine: { lineStyle: { color: '#d9e1ec' } },
      },
      series: ranking ? [{
        name: `差值（${targetLabel} − ${baselineLabel}）`, type: 'bar', barMaxWidth: 22,
        data: rows.map((item) => ({ value: item.delta, itemStyle: { color: item.delta! > 0 ? '#b42318' : item.delta! < 0 ? '#087443' : '#62718a' } })),
        markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: '#62718a', type: 'solid' }, data: [{ xAxis: 0 }] },
      }] : (['baseline', 'target'] as const).map((side) => ({
        id: side, name: side, type: 'bar', barMaxWidth: 18,
        data: rows.map((item) => valid(item[side]) ? item[side] : null),
      })),
    }
    const resize = () => {
      if (!element.clientWidth || !element.clientHeight) return
      if (!chart) { chart = init(element); chart.setOption(option) }
      const left = element.clientWidth < 480 ? 120 : 180
      chart.setOption({ grid: { left }, yAxis: { axisLabel: { width: left - 20 } } })
      chart.resize()
    }
    const observer = new ResizeObserver(resize)
    observer.observe(element)
    resize()
    return () => { observer.disconnect(); chart?.dispose() }
  }, [rows, title, unit, ranking, hasData, displayUnit, deltaUnit, baselineLabel, targetLabel])

  return <section className="compare-chart" aria-label={title}>
    <div className="panel-heading"><h3>{title}</h3><span className="unit">{displayUnit}</span></div>
    {hasData ? <div ref={host} role="img" aria-label={`${title}，详细数值见下方表格`} style={{ height: Math.max(230, rows.length * (ranking ? 32 : 52) + 100) }} />
      : <div className="compare-chart-empty">{ranking ? '当前筛选没有可比较的进程，不生成差值排行。' : '暂无有效数据，不以零值填充。'}</div>}
    <p className="chart-caption">{note} {ranking ? '红色增加、绿色减少，不代表好坏。' : `蓝色为${baselineLabel}，青色为${targetLabel}；缺失不绘柱，零值柱长为零。`} 悬停查看完整名称和数值。</p>
  </section>
}

export function Trend({ path, statisticsPath, active = true, ...props }: Omit<ChartProps, 'data' | 'loading' | 'error'> & {
  path: string | null
  statisticsPath?: string | null
  active?: boolean
}) {
  const [revision, retry] = useRetry()
  const [statisticsRevision, retryStatistics] = useRetry()
  const resource = useResource<SeriesData>(active ? path : null, revision)
  const statistics = useResource<StatisticsData>(active && path ? statisticsPath ?? null : null, statisticsRevision)
  return <div className="trend">
    <SeriesChart {...props} {...resource} percentiles={statisticsPath ? statistics.data?.metrics : props.percentiles} />
    {resource.error && <button className="chart-retry" onClick={retry}>重试此图</button>}
    {statistics.loading && <p role="status">正在读取原始样本分位数，趋势不受影响…</p>}
    {statistics.error && <p className="error" role="alert">原始统计读取失败：{statistics.error} <button onClick={retryStatistics}>重试参考线统计</button></p>}
    {statistics.data && <p className="chart-caption">参考线范围：时间段 {statistics.data.scope.segment ?? '全部'} · {formatTime(statistics.data.start)} → {formatTime(statistics.data.end)} · {formatNumber(statistics.data.samples, 0)} 条原始记录。缩放不重算分位数；参考线不是准入阈值。</p>}
  </div>
}

// 按需加载精确统计，避免打开会话时同时排序所有指标。
export function StatisticsPanel({ title, path, trendPath, disabled }: {
  title: string; path: string | null; trendPath: string | null; disabled: boolean
}) {
  const [open, setOpen] = useState(false)
  const [field, setField] = useState('')
  const [revision, retry] = useRetry()
  const { data, loading, error } = useResource<StatisticsData>(open ? path : null, revision)
  const metric = data?.metrics[field]
  return <section className="panel statistics-panel">
    <div className="panel-heading"><h3>{title}</h3><button disabled={disabled || !path} aria-expanded={open} onClick={() => setOpen(!open)}>{open ? '收起统计' : '全字段统计 / P95 / P99'}</button></div>
    {open && <>
      {loading && <p role="status">正在计算全部原始样本，首次统计较慢，后续使用缓存…</p>}
      {error && <p className="error" role="alert">{error} <button disabled={disabled} onClick={retry}>重试统计</button></p>}
      {data && <>
        <p>时间段 {data.scope.segment ?? '全部'} · {formatNumber(data.samples, 0)} 条原始记录 · {formatTime(data.start)} → {formatTime(data.end)}</p>
        <p className="muted">{data.method}</p>
        <div className="table-scroll"><table><thead><tr><th>指标 / 原始字段</th><th>有效样本</th><th>最小</th><th>均值</th><th>P95</th><th>P99</th><th>最大</th><th>增量累计</th></tr></thead>
          <tbody>{Object.entries(data.metrics).map(([key, item]) => <tr key={key}>
            <td><strong>{item.label}</strong><small>{key}</small>{item.note && <small>{item.note}</small>}</td><td>{formatNumber(item.count, 0)}</td>
   {(['min', 'avg', 'p95', 'p99', 'max'] as const).map((stat) => <td key={stat}>{formatStatistic(item[stat], item.unit)}</td>)}
            <td>{formatStatistic(item.total, item.unit, true)}</td>
          </tr>)}</tbody></table></div>
        <label className="metric-select">查看任意数值指标趋势 <select value={field} disabled={disabled} onChange={(event) => setField(event.target.value)}>
          <option value="">选择指标</option>{Object.entries(data.metrics).map(([key, item]) => <option key={key} value={key}>{item.label} · {key}</option>)}
        </select></label>
        {metric && <Trend path={trendPath} percentiles={data.metrics} title={metric.label} unit={metric.unit} metrics={[{ field, label: metric.label }]} mode="scatter" note={metric.note || '全部数值字段均可查看；缺失不补零。'} />}
        {Object.entries(data.categories).map(([key, category]) => <details key={key}>
          <summary>{category.label}（{key}）· {formatNumber(category.count, 0)} 个有效样本</summary>
          <p>{category.note || '分类频数按原始样本计算，不代表时间占比。'}</p>
          <div className="table-scroll"><table><thead><tr><th>分类值</th><th>样本数</th><th>样本占比 %</th></tr></thead><tbody>
            {category.values.map((item) => <tr key={JSON.stringify(item.value)}><td>{String(item.value)}</td><td>{formatNumber(item.count, 0)}</td><td>{formatNumber(item.percent, 2)}</td></tr>)}
          </tbody></table>{!category.values.length && <p>无有效分类样本。</p>}</div>
        </details>)}
      </>}
    </>}
  </section>
}

// 重试只影响当前图，避免重新请求所有明细。
import { useState } from 'react'
function useRetry(): [number, () => void] {
  const [revision, setRevision] = useState(0)
  return [revision, () => setRevision((value) => value + 1)]
}