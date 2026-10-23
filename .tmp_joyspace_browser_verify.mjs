import { resolveAuth } from './joyspace_assets/joyspace-kit/skills/markdown-to-joyspace/scripts/import_markdown_doc.js';
const root = 'http://127.0.0.1:9334';
const mode = process.argv[2];
const info = await (await fetch(root + '/json/version')).json();
const ws = new WebSocket(info.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let seq = 0;
const net = new Map();
const events = [];
const pending = new Map();
ws.onmessage = ({ data }) => {
  const msg = JSON.parse(data);
  const p = msg.params || {};
  if (msg.method === 'Network.requestWillBeSent') {
    const u = new URL(p.request.url);
    net.set(p.requestId, u.origin + u.pathname);
  }
  if (msg.method === 'Network.webSocketCreated') {
    const u = new URL(p.url);
    events.push({ event: 'ws-created', url: u.origin + u.pathname });
  }
  if (msg.method === 'Network.webSocketFrameReceived') {
    const raw = p.response.payloadData;
    const start = raw.indexOf('[');
    let frame;
    try { frame = JSON.parse(raw.slice(start)); } catch {}
    events.push({ event: 'ws-frame', prefix: raw.slice(0, 2), size: raw.length,
      name: Array.isArray(frame) && typeof frame[0] === 'string' ? frame[0] : undefined,
      eventName: frame?.[1]?.eventName, meta: frame?.[1]?.meta,
      messageType: typeof frame?.[1]?.message,
      messageKeys: frame?.[1]?.message && typeof frame[1].message === 'object' ? Object.keys(frame[1].message) : undefined,
      keys: Array.isArray(frame) && frame[1] && typeof frame[1] === 'object' ? Object.keys(frame[1]) : undefined });
  }
  if (msg.method === 'Network.webSocketFrameError') events.push({ event: 'ws-error', error: p.errorMessage });
  if (msg.method === 'Network.responseReceived' && p.response.status >= 400) events.push({ url: net.get(p.requestId), status: p.response.status });
  if (msg.method === 'Network.loadingFailed') events.push({ url: net.get(p.requestId), error: p.errorText });
  const waiter = pending.get(msg.id);
  if (waiter) {
    pending.delete(msg.id);
    msg.error ? waiter.reject(new Error(msg.error.message)) : waiter.resolve(msg.result);
  }
};
function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, sessionId }));
  });
}
try {
  if (mode === 'close') {
    await send('Browser.close');
  } else {
    let target;
    if (mode === 'init') {
      const { browserContextId } = await send('Target.createBrowserContext');
      target = await send('Target.createTarget', { url: 'about:blank', browserContextId });
    } else {
      const { targetInfos } = await send('Target.getTargets');
      target = targetInfos.find(t => t.type === 'page' && t.url.startsWith('https://joyspace.jd.com/pages/'));
      if (!target) throw new Error('Isolated test page not found');
    }
    const { sessionId } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
    await new Promise(r => setTimeout(r, 300));
    if (mode === 'init' || mode === 'auth') {
      const auth = await resolveAuth({ tenantCode: 'CN.JD.GROUP', deviceId: 'noDeviceId' });
      const cookies = [];
      for (const url of ['https://joyspace.jd.com', 'https://apijoyspace.jd.com', 'https://otjoyspace.jd.com']) {
        if (auth.meToken) cookies.push({ name: 'me_token', value: auth.meToken, url, secure: true, httpOnly: true });
        if (auth.ssoToken) cookies.push({ name: 'sso.jd.com', value: auth.ssoToken, url, secure: true, httpOnly: true });
      }
      await send('Network.setCookies', { cookies }, sessionId);
      await send('Page.navigate', { url: 'https://joyspace.jd.com/pages/NVhd761iEueS8Z3qaMWE' }, sessionId);
      await new Promise(r => setTimeout(r, 15000));
    }
    if (mode === 'reload') {
      await send('Network.enable', {}, sessionId);
      await send('Page.reload', {}, sessionId);
      await new Promise(r => setTimeout(r, 25000));
      console.log(JSON.stringify({ events }));
    }
    if (mode === 'viewport') {
      await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
    }
    if (mode === 'click') {
      const point = JSON.parse(process.argv[3]);
      await send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...point }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', clickCount: 1 }, sessionId);
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', clickCount: 1 }, sessionId);
      await new Promise(r => setTimeout(r, 1000));
    }
    if (mode === 'observe') await send('Network.enable', {}, sessionId);
    const expression = (mode === 'click' ? null : process.argv[3]) || 'JSON.stringify({url:location.origin+location.pathname,text:document.body.innerText.slice(0,14000),editables:document.querySelectorAll("[contenteditable=true]").length})';
    const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    console.log(result.result?.value ?? result.result?.description);
    if (mode === 'observe') {
      await new Promise(r => setTimeout(r, 12000));
      console.log(JSON.stringify({ requests: [...net.values()], events }));
    }
    await send('Target.detachFromTarget', { sessionId });
  }
} finally {
  ws.close();
}