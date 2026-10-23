// Private Chromium process over a pipe: never attach to a user's browser.
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function findBrowser() {
  const candidates = process.env.JOYSPACE_BROWSER_PATH ? [process.env.JOYSPACE_BROWSER_PATH] : {
    darwin: ['/Applications', path.join(os.homedir(), 'Applications')].flatMap(root =>
      ['Google Chrome', 'Google Chrome Dev', 'Microsoft Edge'].map(name =>
        path.join(root, name + '.app/Contents/MacOS', name))),
    win32: [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA]
      .filter(Boolean).flatMap(root => [path.join(root, 'Google/Chrome/Application/chrome.exe'),
        path.join(root, 'Microsoft/Edge/Application/msedge.exe')]),
    linux: ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable',
      '/usr/bin/chromium', '/usr/bin/chromium-browser'],
  }[process.platform] || [];
  for (const file of candidates) {
    if (!path.isAbsolute(file)) continue;
    try {
      if (!(await fs.stat(file)).isFile()) continue;
      await fs.access(file, process.platform === 'win32' ? fs.constants.F_OK : fs.constants.X_OK);
      return file;
    } catch { /* Try the next installed browser. */ }
  }
  return null;
}

// This function runs inside the isolated page; keep it self-contained.
export function editorAction({ pageId, title, initial, final, action }) {
  if (location.origin !== 'https://joyspace.jd.com' || location.pathname !== '/pages/' + pageId) {
    if (action === 'check') return false;
    throw new Error('Unexpected editor location');
  }
  const clean = value => {
    if (Array.isArray(value)) return value.map(clean);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.keys(value).sort().filter(k => k !== 'id')
      .map(k => [k, clean(value[k])]));
  };
  const equal = (a, b) => JSON.stringify(clean(a)) === JSON.stringify(clean(b));
  const element = document.querySelector('.slate-editor');
  if (!element) return false;
  let fiber = element[Object.keys(element).find(k =>
    k.startsWith('__reactInternalInstance$') || k.startsWith('__reactFiber$'))];
  let editor;
  for (let n = 0; fiber && n < 100; n++, fiber = fiber.return) {
    if (fiber.memoizedProps?.editor?.apply) { editor = fiber.memoizedProps.editor; break; }
  }
  if (!editor || !Array.isArray(editor.children)) return false;
  const expected = action === 'insert' ? initial : final;
  const offset = editor.children.length - expected.length;
  // Only the server's optional title paragraph may precede our report.
  const prefix = editor.children[0];
  const titlePrefix = prefix?.type === 'p' && typeof title === 'string' && title.length > 0
    && Array.isArray(prefix.children) && prefix.children.length > 0
    && prefix.children.every(leaf => typeof leaf.text === 'string' && !leaf.children && !leaf.type)
    && prefix.children.map(leaf => leaf.text).join('') === title;
  if (offset < 0 || offset > 1 || (offset === 1 && !titlePrefix)
      || !equal(editor.children.slice(offset), expected)) return false;
  if (action === 'insert') {
    const prefix = JSON.stringify(editor.children.slice(0, offset));
    for (let i = 0; i < final.length; i++) {
      const node = final[i];
      if (node.type !== 'img') continue;
      if (!editor.isVoid?.(node)) throw new Error('Unsupported image editor');
      editor.apply({ type: 'insert_node', path: [offset + i], node });
    }
    if (JSON.stringify(editor.children.slice(0, offset)) !== prefix
        || !equal(editor.children.slice(offset), final)) {
      throw new Error('Editor changed report unexpectedly; do not retry');
    }
    return true;
  }
  return true;
}

export async function startBrowser(executable, signal) {
  signal?.throwIfAborted();
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), 'jdperf-charts-'));
  const child = spawn(executable, ['--headless=new', '--remote-debugging-pipe',
    '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'], windowsHide: true });
  let sequence = 0, buffer = '', sessionId, closed = false, disconnected = false;
  let closing;
  const exited = new Promise(resolve => child.once('close', resolve));
  const pending = new Map();
  const fail = () => {
    disconnected = true;
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('Private browser disconnected')); }
    pending.clear();
  };
  child.on('error', fail);
  child.on('exit', fail);
  const stop = () => { if (child.exitCode === null) child.kill(); };
  process.once('exit', stop);
  child.stdio[4].setEncoding('utf8');
  child.stdio[4].on('data', chunk => {
    buffer += chunk;
    let end;
    while ((end = buffer.indexOf('\0')) >= 0) {
      const raw = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(raw); } catch { continue; }
      const item = pending.get(message.id);
      if (!item) continue;
      pending.delete(message.id); clearTimeout(item.timer);
      message.error ? item.reject(new Error('Private browser command failed')) : item.resolve(message.result);
    }
  });
  child.stdio[3].on('error', fail);
  function send(method, params = {}, session) {
    return new Promise((resolve, reject) => {
      if (method !== 'Browser.close') signal?.throwIfAborted();
      if (closed || disconnected || child.exitCode !== null || child.signalCode !== null) return reject(new Error('Private browser closed'));
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('Private browser timed out')); }, 12000);
      pending.set(id, { resolve, reject, timer });
      child.stdio[3].write(JSON.stringify({ id, method, params, sessionId: session }) + '\0');
    });
  }
  async function evaluate(fn, args) {
    const result = await send('Runtime.evaluate', {
      expression: '(' + fn.toString() + ')(' + JSON.stringify(args) + ')',
      awaitPromise: true, returnByValue: true,
    }, sessionId);
    if (result.exceptionDetails) throw new Error('Private editor validation failed');
    return result.result?.value;
  }
  async function waitFor(check) {
    for (let n = 0; n < 45; n++) {
      if (await check()) return;
      await delay(500);
    }
    throw new Error('Private editor not ready or report differs');
  }
  function close() {
    if (closing) return closing;
    closing = (async () => {
      try { await send('Browser.close'); } catch { /* Stop only our private child. */ }
      closed = true; stop(); fail();
      child.stdio[3].destroy(); child.stdio[4].destroy();
      const force = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      }, 2000);
      await exited;
      clearTimeout(force);
      process.removeListener('exit', stop);
      process.removeListener('SIGTERM', terminate);
      process.removeListener('SIGINT', terminate);
      signal?.removeEventListener('abort', abort);
      // Cookies only live in the off-the-record context, never in this profile.
      await fs.rm(profile, { recursive: true, force: true }).catch(() => {});
    })();
    return closing;
  }
  const terminate = () => { close().finally(() => process.exit(1)); };
  process.once('SIGTERM', terminate);
  process.once('SIGINT', terminate);
  const abort = () => { fail(); void close(); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    signal?.throwIfAborted();
    const { browserContextId } = await send('Target.createBrowserContext');
    const { targetId } = await send('Target.createTarget', { url: 'about:blank', browserContextId });
    ({ sessionId } = await send('Target.attachToTarget', { targetId, flatten: true }));
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false,
    }, sessionId);
  } catch (error) { await close(); throw error; }
  return {
    async authenticate(auth) {
      const cookies = [];
      for (const url of ['https://joyspace.jd.com', 'https://apijoyspace.jd.com', 'https://otjoyspace.jd.com']) {
        if (auth.meToken) cookies.push({ name: 'me_token', value: auth.meToken, url, secure: true, httpOnly: true });
        if (auth.ssoToken) cookies.push({ name: 'sso.jd.com', value: auth.ssoToken, url, secure: true, httpOnly: true });
      }
      await send('Network.setCookies', { cookies }, sessionId);
    },
    async open(pageId) {
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(pageId)) throw new Error('Invalid page id');
      await send('Page.navigate', { url: 'https://joyspace.jd.com/pages/' + pageId }, sessionId);
    },
    async insertCharts(pageId, initial, final, title) {
      // Poll readiness only. The mutating operation is issued exactly once.
      await waitFor(() => evaluate(editorAction, { pageId, title, final: initial, action: 'check' }));
      if (!await evaluate(editorAction, { pageId, title, initial, final, action: 'insert' })) {
        throw new Error('Report changed before insertion');
      }
    },
    async verifyImages(pageId, final, title) {
      await send('Page.reload', { ignoreCache: true }, sessionId);
      await waitFor(() => evaluate(editorAction, { pageId, title, final, action: 'check' }));
      for (const image of final.filter(block => block.type === 'img')) {
        const fileId = image.url.split('/').at(-2);
        await waitFor(() => evaluate(({ fileId }) => {
          const img = [...document.images].find(node => {
            try { return new URL(node.src).pathname.includes('/files/' + fileId + '/link'); }
            catch { return false; }
          });
          if (!img) return false;
          img.scrollIntoView({ block: 'center' });
          return img.complete && img.naturalWidth === 1200 && img.naturalHeight === 400;
        }, { fileId }));
      }
    },
    close,
  };
}