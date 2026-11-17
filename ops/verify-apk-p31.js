#!/usr/bin/env node
/**
 * P31 前端改动的可复现验证（不依赖真机、不装第三方包）。
 *
 * 做法：把 mobile/android/assets/index.html 里那段真实 <script> 原样取出来，
 * 在一个最小 DOM/原生桩里跑，然后**调用真实函数**看真实产出。
 * 验的是代码逻辑，不是"看起来对"。
 *
 * 跑法：node ops/verify-apk-p31.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'mobile/android/assets/index.html');
const INPUT_JS = path.join(ROOT, 'mobile/android/assets/mobile-input.js');

let failures = 0;
function check(name, ok, detail) {
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (detail ? '  → ' + detail : ''));
  if (!ok) failures += 1;
}

// ---------------------------------------------------------------- 最小 DOM
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), children: [], dataset: {}, style: {},
    _classes: new Set(), value: '', textContent: '', innerHTML: '', hidden: false,
    scrollHeight: 46, scrollTop: 0, clientHeight: 100, checked: false, disabled: false,
    set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...this._classes].join(' '); },
    classList: {
      add: (c) => el._classes.add(c), remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c),
      toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    append(...kids) { for (const k of kids) { k.parent = el; el.children.push(k); } },
    replaceChildren(...kids) { el.children = []; el.append(...kids); },
    remove() { if (el.parent) el.parent.children = el.parent.children.filter((c) => c !== el); },
    querySelector() { return null; },
    querySelectorAll(sel) { return queryAll(el, sel); },
    blur() {}, focus() {}, click() { if (el.onclick) el.onclick({ target: el }); },
    matches() { return false; },
  };
  return el;
}
function walk(node, out) {
  for (const c of node.children || []) { out.push(c); walk(c, out); }
  return out;
}
function queryAll(root, sel) {
  // 只支持本验证用到的选择器：.class 与 .a.b 组合
  const parts = String(sel).trim().split('.').filter(Boolean);
  return walk(root, []).filter((el) => parts.every((p) => el._classes && el._classes.has(p)));
}

function makePage() {
  const ids = ['chat', 'text', 'send', 'pick', 'attachments', 'error', 'composeHint', 'typing',
    'lifePage', 'lifeContent', 'lifeUpdated', 'lifeReadError', 'runtimeContent', 'runtimeReadError',
    'runtimeUpdated', 'runtimeOverlay', 'overlay', 'pair', 'code', 'pairError', 'pairButton',
    'settings', 'closeSettings', 'gallery', 'closeGallery', 'speech', 'phone', 'version',
    'appInfo', 'battery', 'reconnect', 'tabChat', 'tabLife', 'tabSettings', 'meta', 'speechStatus',
    'openRuntime', 'closeRuntime', 'refreshRuntime', 'imQqEnabled', 'imQqUrl', 'imQqState',
    'imSummary', 'imQqSave', 'imQqToken'];
  const byId = {};
  for (const id of ids) byId[id] = makeEl(id === 'text' ? 'textarea' : 'div');
  byId.chat.dataset = {};
  const document = {
    getElementById: (id) => byId[id] || null,
    createElement: (t) => makeEl(t),
    createDocumentFragment: () => makeEl('fragment'),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, activeElement: null, hidden: false,
  };
  return { byId, document };
}

// ---------------------------------------------------------------- 跑真实脚本
function loadApp() {
  const html = fs.readFileSync(HTML, 'utf8');
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('index.html 里没找到行内 <script>');
  const page = makePage();
  const storage = new Map();
  const calls = [];
  const sandbox = {
    document: page.document,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    // 定时器必须保留语义：index.html 的 api() 用 setTimeout(…,45000) 做超时，
    // 桩里若「立即执行所有定时器」，真实请求还没回填就被判超时（这是桩的错，不是产品代码的错）。
    setTimeout: (fn, delay) => {
      if (typeof delay === 'number' && delay >= 1000) return 0; // 长的超时定时器不触发
      return global.setTimeout(fn, 0);
    },
    setInterval: () => 0, clearInterval() {}, clearTimeout() {},
    console, JSON, Date, Math, String, Number, Array, Object, RegExp, Promise, Error, Set, Map,
    Native: {
      stopSpeech() {}, pickImage() {}, speak() {}, startConnection() {}, openPhoneSettings() {},
      openAppSettings() {}, openSettings() {}, setSpeech() {}, pickImageResult() {},
      // index.html 的 api() 走 Native.request；实现这层桥，让**真实的 api() 跑起来**
      // （这比替换 api 更接近真机：被验证的是产品代码，不是我的桩）。
      request(id, method, url, bodyText) {
        calls.push({ method, url, body: bodyText ? JSON.parse(bodyText) : null });
        sandbox.queue.push({ id });
      },
    },
    fetch: async () => ({ json: async () => ({}) }),
    queue: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html<script>' });
  return { sandbox, page, calls, storage };
}

// 让 Native.request 的「服务端回应」落地：index.html 的入口是 onNativeResult(id,status,body)。
function answerPending(sandbox, payload, status) {
  const fn = ['onNativeResult', 'onNativeResponse', 'onResponse'].find((n) => typeof sandbox[n] === 'function');
  if (!fn) return null;
  while (sandbox.queue.length) {
    const { id } = sandbox.queue.shift();
    const body = typeof payload === 'string' ? payload : JSON.stringify(payload);
    sandbox[fn](id, status === undefined ? 202 : status, body);
  }
  return fn;
}

// ---------------------------------------------------------------- 用例
console.log('=== P31-3 运行详情：refresh 必须带 runtime=1 ===');
{
  const js = fs.readFileSync(INPUT_JS, 'utf8');
  const openLine = js.split('\n').find((l) => l.includes("$('openRuntime').onclick"));
  const refreshLine = js.split('\n').find((l) => l.includes("$('refreshRuntime').onclick"));
  check('openRuntime 调 refresh(true)', /refresh\(true\)/.test(openLine || ''), openLine && openLine.trim().slice(0, 90));
  check('refreshRuntime 调 refresh(true)', /refresh\(true\)/.test(refreshLine || ''), refreshLine && refreshLine.trim().slice(0, 90));
  check('没有残留的裸 refresh()', !/\brefresh\(\)/.test(js), 'mobile-input.js 全文');
}

console.log('=== P31-2 草稿：只存文字，能恢复 ===');
{
  const app = loadApp();
  const { sandbox, page, storage } = app;
  sandbox.$ = page.document.getElementById;
  page.byId.text.value = '这是一段没发出去的草稿';
  vm.runInContext('saveDraft()', sandbox);
  const saved = storage.get('xiaoyu.draft.v1');
  check('输入后写进 localStorage', saved === '这是一段没发出去的草稿', saved);

  page.byId.text.value = '';
  vm.runInContext('restoreDraft()', sandbox);
  check('页面重建后恢复草稿', page.byId.text.value === '这是一段没发出去的草稿', page.byId.text.value);

  page.byId.text.value = '用户正在写的新内容';
  vm.runInContext('restoreDraft()', sandbox);
  check('不覆盖用户正在输入的内容', page.byId.text.value === '用户正在写的新内容', page.byId.text.value);

  page.byId.text.value = '   ';
  vm.runInContext('saveDraft()', sandbox);
  check('空白草稿会被清掉', storage.get('xiaoyu.draft.v1') === undefined, String(storage.get('xiaoyu.draft.v1')));

  const src = fs.readFileSync(HTML, 'utf8');
  check('草稿不含图片（图片不持久化）', !/DRAFT_KEY[^;]*images/.test(src) && !/saveDraft[\s\S]{0,200}images=/.test(src),
    '草稿路径不引用 images 数组');
  check('草稿有长度上限', /DRAFT_LIMIT/.test(src) && /DRAFT_LIMIT\s*=\s*\d+/.test(src), 'DRAFT_LIMIT');
}

console.log('=== P31-1 消息状态：状态词与来源 ===');
{
  const src = fs.readFileSync(HTML, 'utf8');
  check('读的是 state.mobile_requests（不是 companion_actions）',
    /mobile_requests/.test(src) && !/companion_actions/.test(src), 'index.html');
  for (const word of ['sending', 'accepted', 'completed', 'failed', 'interrupted']) {
    check('状态覆盖 ' + word, new RegExp(word + ':').test(src), '');
  }
  check('区分「服务器收下」与「她处理完」',
    /不代表她回过/.test(src) || /不一定回/.test(src) || /她在处理/.test(src),
    '注释/文案里点明差别');
}

console.log('=== P31-1 原地重试：同一 id（关键边界）===');
(async () => {
  const app = loadApp();
  const { sandbox, page, calls } = app;
  sandbox.$ = page.document.getElementById;
  page.byId.chat.children = [];
  // busy/error 是脚本内的 let 绑定，从外部注入不到；用脚本自己的作用域设置。
  vm.runInContext(
    "pendingRequests=[{id:'req-1',text:'你好',status:'failed',at:Date.now(),payload:{text:'你好'}}];" +
    "saveRequests(pendingRequests);busy=false;error=function(){};refresh=function(){};",
    sandbox);

  const pendingCall = vm.runInContext("resend('req-1')", sandbox);
  for (let i = 0; i < 3; i += 1) await new Promise((r) => setImmediate(r));
  // api() 走 Native.request；这里对每一条挂起的请求按 202 回填，promise 才落地。
  while (sandbox.queue.length) {
    const { id } = sandbox.queue.shift();
    sandbox.onNativeResult(id, 202, JSON.stringify({ id: 'req-1', accepted: true }));
  }
  await pendingCall;
  for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));

  const sent = calls.filter((c) => c.method === 'POST' && c.url === '/mobile/chat');
  check('resend 真的发出了请求', sent.length === 1, sent.length + ' 次');
  check('重试沿用同一个 id（服务端去重键不变）',
    !!(sent[0] && sent[0].body && sent[0].body.id === 'req-1'),
    sent[0] && JSON.stringify(sent[0].body));

  const rows = vm.runInContext('pendingRequests', sandbox);
  const row = rows.find((r) => r.id === 'req-1');
  check('重试后本地状态推进到 accepted（不是悄悄停在 failed）',
    row && row.status === 'accepted', row && row.status);

  // 服务端那侧的真实行为：把 314-317 的分支逻辑原样复现出来，证明前端的用法会踩坑。
  console.log('=== 服务端去重语义（复现 companion_mobile.py:314-317）===');
  const serverSide = (old) => {
    if (!old) return 'new';
    if (old.digest !== 'same') return '409';
    return 'duplicate:' + old.status;
  };
  check('同 id 同内容 + failed  → 服务端返回 duplicate:failed（不会重跑）',
    serverSide({ digest: 'same', status: 'failed' }) === 'duplicate:failed',
    serverSide({ digest: 'same', status: 'failed' }));
  check('同 id 同内容 + completed → duplicate:completed（不重跑）',
    serverSide({ digest: 'same', status: 'completed' }) === 'duplicate:completed',
    serverSide({ digest: 'same', status: 'completed' }));
  check('同 id 不同内容 → 409（现有行为）', serverSide({ digest: 'diff', status: 'failed' }) === '409',
    serverSide({ digest: 'diff', status: 'failed' }));
  console.log('  NOTE  上面第二条说明：纯前端"沿用同一 id 重试"目前拿不回重跑，需服务端配合（待队长裁定后实现）');

  report();
})();

function report() {
  console.log('');
  if (failures) { console.log('RESULT: FAILED  (' + failures + ' 项)'); process.exit(1); }
  console.log('RESULT: OK  (前端改动全部通过)');
  process.exit(0);
}
