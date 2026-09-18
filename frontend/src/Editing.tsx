import { createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { errorMessage } from './api'

type EditState = { dirty: boolean; busy: boolean }
type Guard = EditState & {
  update: (key: string, state?: EditState) => void
  acquire: (key: string) => boolean
  confirm: (message?: string) => boolean
}
export const EditContext = createContext<Guard | null>(null)

export function useWorkspaceGuard(): Guard {
  const entries = useRef(new Map<string, EditState>())
  const [state, setState] = useState<EditState>({ dirty: false, busy: false })
  const update = useCallback((key: string, value?: EditState) => {
    if (value) entries.current.set(key, value)
    else entries.current.delete(key)
    const values = [...entries.current.values()]
    const next = { dirty: values.some((item) => item.dirty), busy: values.some((item) => item.busy) }
    setState((previous) => previous.dirty === next.dirty && previous.busy === next.busy ? previous : next)
  }, [])
  const confirm = useCallback((message = '有未保存的配置或成员选择，继续将放弃这些改动。是否继续？') => {
    const values = [...entries.current.values()]
    if (values.some((item) => item.busy)) return false
    return !values.some((item) => item.dirty) || window.confirm(message)
  }, [])
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if ([...entries.current.values()].some((item) => item.dirty || item.busy)) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', beforeUnload)
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [])
  const acquire = useCallback((key: string) => {
    if ([...entries.current.values()].some((item) => item.busy)) return false
    update(key, { dirty: false, busy: true })
    return true
  }, [update])
  return { ...state, update, acquire, confirm }
}

export function useEditGuard() {
  const context = useContext(EditContext)
  if (!context) throw new Error('编辑保护未初始化')
  return context
}

export function useDirty(dirty: boolean) {
  const key = useId()
  const { update } = useEditGuard()
  useLayoutEffect(() => {
    update(key, { dirty, busy: false })
    return () => update(key)
  }, [dirty, key, update])
}

// 写请求串行；卸载时取消，并且不让旧请求回写到新会话。
export function useMutation() {
  const key = useId()
  const { update, acquire } = useEditGuard()
  const controller = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => () => { controller.current?.abort(); update(key) }, [key, update])
  async function run(action: (signal: AbortSignal) => Promise<void>) {
    if (controller.current || !acquire(key)) return
    const request = new AbortController()
    controller.current = request
    setBusy(true)
    setError('')
    try { await action(request.signal) }
    catch (failure) { if (!request.signal.aborted) setError(errorMessage(failure)) }
    finally {
      if (controller.current === request) {
        controller.current = null
        if (!request.signal.aborted) {
          update(key)
          setBusy(false)
        }
      }
    }
  }
  return { busy, error, setError, run }
}