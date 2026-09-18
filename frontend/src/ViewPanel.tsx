import { useState } from 'react'
import type { ReactNode } from 'react'

// 首次进入时挂载，之后只隐藏：保留搜索、成员选择和表单草稿。
export function ViewPanel({ active, children }: { active: boolean; children: ReactNode }) {
  const [visited, setVisited] = useState(active)
  if (active && !visited) setVisited(true)
  return <div hidden={!active}>{(active || visited) && children}</div>
}