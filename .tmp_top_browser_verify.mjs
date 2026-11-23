import assert from 'node:assert/strict';
const info = await (await fetch('http://127.0.0.1:9335/json/version')).json();
const ws = new WebSocket(info.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let seq = 0;
const pending = new Map(), errors = [];
ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  if (msg.method === 'Runtime.exceptionThrown') errors.push(msg.params.exceptionDetails.text);
  const p = pending.get(msg.id);
  if (p) { pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result); }
};
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, sessionId }));
  });
}
const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
const evaluate = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)).result.value;
try {
  await send('Runtime.enable', {}, sessionId);
  await send('Page.navigate', { url: 'http://127.0.0.1:8765' }, sessionId);
  let result;
  for (let i = 0; i < 45; i++) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    await evaluate(`(() => { const s = document.querySelector('section[aria-label="Android设备采集"] select'); if (s && s.value !== '8ef5a5cd' && [...s.options].some(o => o.value === '8ef5a5cd')) { s.value = '8ef5a5cd'; s.dispatchEvent(new Event('change', { bubbles: true })); } })()`);
    result = await evaluate(`(() => { const s = document.querySelector('section[aria-label="独立 Top 采集"]'); if (!s) return null; return { buttons: [...s.querySelectorAll('button')].map(b => ({text:b.textContent,disabled:b.matches(':disabled')})), text:s.innerText, oldEnabled:document.querySelector('.device-check input')?.checked, alerts:[...document.querySelectorAll('[role="alert"]')].map(e=>e.innerText) }; })()`);
    if (result?.buttons.some(b => b.text === '开始采集' && !b.disabled)) break;
  }
  console.log(JSON.stringify({ result, errors }, null, 2));
  assert.equal(result.oldEnabled, false);
  assert.ok(result.buttons.some(b => b.text === '开始采集' && !b.disabled));
  assert.ok(result.text.includes('已采样：15'));
  assert.equal(errors.length, 0);
} finally {
  await send('Browser.close');
  ws.close();
}