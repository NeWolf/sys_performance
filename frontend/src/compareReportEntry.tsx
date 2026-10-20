import { createRoot } from 'react-dom/client'
import { Results } from './CompareResults'
import type { ComparisonReport } from './CompareResults'
import baseStyles from './index.css?inline'
import appStyles from './App.css?inline'

const style = document.createElement('style')
style.textContent = baseStyles + appStyles + '\nbody { min-width: 360px; } main { max-width: 1600px; padding: 24px; margin: auto; } h1 { margin-bottom: 16px; }'
document.head.append(style)
const payload = JSON.parse(document.getElementById('comparison-data')!.textContent!) as ComparisonReport
createRoot(document.getElementById('root')!).render(<main>
  <h1>双次采集对比报告</h1>
  <p className="muted">导出时间：{payload.generatedAt} · 本报告可离线使用；包含全部比较数据，当前筛选可调整。</p>
  <Results data={payload.data} baselineLabel={payload.baselineLabel} targetLabel={payload.targetLabel} initialView={payload.view} />
</main>)