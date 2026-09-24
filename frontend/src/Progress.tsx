import { formatNumber } from './api'
import type { Progress } from './api'

const stageLabels: Record<string, string> = {
  waiting: '等待分析任务', cache: '检查本机分析缓存', read_cache: '命中缓存 · 读取分析报告',
  validate: '校验采集', prepare: '读取原始样本', timeline: '整理周期与采样间隔',
  system: '汇总系统样本', process: '汇总进程样本', io: '合计进程 IO',
  statistics: '排序并计算精确分位数', active_statistics: '计算活跃进程统计',
  difference: '匹配进程并计算差值', assemble: '组装分析报告', render: '渲染交互报告',
  compress: '压缩报告数据', save: '保存本机分析缓存', complete: '分析完成',
}

export function AnalysisProgress({ progress, elapsed, report = false, baselineLabel = '基准', targetLabel = '对比' }: { progress?: Progress; elapsed: number; report?: boolean; baselineLabel?: string; targetLabel?: string }) {
  const statistics = progress?.stage === 'statistics' || progress?.stage === 'active_statistics'
  const total = progress?.total
  const completed = progress?.completed
  const counted = !statistics && total != null && Number.isFinite(total) && total >= 0 && completed != null && Number.isFinite(completed) && completed >= 0
  const percent = counted && total > 0 ? Math.min(100, Math.floor(completed / total * 100)) : undefined
  const side = progress?.side === 'baseline' ? `${baselineLabel}（1/2） · ` : progress?.side === 'target' ? `${targetLabel}（2/2） · ` : ''
  const stage = progress ? report && progress.stage === 'prepare' ? '生成分析报告 · 读取原始样本' : stageLabels[progress.stage] ?? '处理中' : '连接分析服务'
  return <div className="banner info compare-progress" role="status" aria-live="polite">
    <strong>{side}{stage}</strong><span>已用时 {elapsed} 秒</span>
    <progress aria-label={`${side}${stage}阶段进度`} max={100} value={percent} />
    <small>{counted ? `${formatNumber(completed, 0)} / ${formatNumber(total, 0)} ${progress?.stage === 'difference' ? '个进程' : '条记录'}${percent == null ? '' : ` · 当前阶段 ${percent}%`}` : '此阶段无法预估百分比，请稍候。'} 精确分位数排序可能较慢，进度不是总耗时比例。</small>
    {report && <small>{progress?.stage === 'read_cache' ? '已命中本机缓存，无需重新分析原始日志。' : progress?.stage === 'waiting' ? '等待已有任务结束，连接保持中。' : progress?.stage === 'cache' || !progress ? '正在检查缓存；首次分析或版本更新时将生成报告。' : '根据服务端实际处理阶段更新，不预估总完成时间。'}</small>}
  </div>
}