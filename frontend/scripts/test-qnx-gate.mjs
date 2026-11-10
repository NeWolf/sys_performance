import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// 离线组件状态测试，不访问设备或后端，不模拟浏览器视觉效果。
const source = readFileSync(new URL('../src/Qnx.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
}).outputText
const states = []
let cursor = 0
const jsx = (type, props) => ({ type, props: props || {} })
const mutation = { busy: false, error: '', setError() {}, run() { throw new Error('Unexpected operation') } }
const modules = {
  react: { useState(initial) {
    const index = cursor++
    if (!(index in states)) states[index] = initial
    return [states[index], (value) => { states[index] = typeof value === 'function' ? value(states[index]) : value }]
  } },
  'react/jsx-runtime': { jsx, jsxs: jsx, Fragment: 'fragment' },
  './api': {},
  './Editing': { useMutation: () => mutation, useEditGuard: () => ({}) },
}
const context = { exports: {}, require(name) { assert.ok(name in modules); return modules[name] } }
vm.runInNewContext(compiled, context)
const props = {
  serial: '', connected: false, locked: false, visible: true,
  running: { current: false }, polling: { current: null }, topPolling: { current: null },
  onBusy() {}, onImported() {},
}
function render() { cursor = 0; return context.exports.Qnx(props) }
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return []
  if (Array.isArray(tree)) return tree.flatMap(nodes)
  return [tree, ...nodes(tree.props?.children)]
}
function find(tree, predicate) { return nodes(tree).find(predicate) }
const toggle = (tree) => find(tree, (node) => node.props['aria-controls'] === 'qnx-capture-content')
const form = (tree) => find(tree, (node) => node.type === 'form')
const password = (tree) => find(tree, (node) => node.props.type === 'password')
const start = (tree) => find(tree, (node) => node.props.children === '开始 QNX 采集')
const submit = (tree) => form(tree).props.onSubmit({ preventDefault() {} })
let tree = render()
assert.equal(toggle(tree).props['aria-expanded'], false)
assert.equal(start(tree), undefined)
assert.equal(form(tree), undefined)
toggle(tree).props.onClick()
tree = render()
assert.ok(password(tree))
password(tree).props.onChange({ target: { value: 'wrong' } })
submit(render())
tree = render()
assert.equal(start(tree), undefined)
assert.equal(password(tree).props.value, '')
assert.ok(find(tree, (node) => node.props.role === 'alert'))
password(tree).props.onChange({ target: { value: '666888' } })
submit(render())
tree = render()
assert.equal(toggle(tree).props['aria-expanded'], true)
assert.ok(start(tree))
assert.equal(form(tree), undefined)
mutation.busy = true
assert.equal(toggle(render()).props.disabled, true)
mutation.busy = false
toggle(render()).props.onClick()
tree = render()
assert.equal(start(tree), undefined)
toggle(tree).props.onClick()
tree = render()
assert.equal(password(tree).props.value, '')
assert.equal(start(tree), undefined)
toggle(tree).props.onClick()
assert.equal(form(render()), undefined)
states.length = 0
assert.equal(toggle(render()).props['aria-expanded'], false)
console.log('QNX gate: default lock, wrong/correct password, relock, cancel, busy and remount passed')