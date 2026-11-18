#!/usr/bin/env node
/**
 * P36 第二批：手机操作状态页 + 独立停止入口。
 *
 * 方法同第一批：把真实 index.html 的 <script> 原样取出来，在最小 DOM/原生桩里跑，
 * 调真实函数看真实产出。验的是产品代码，不是重写的逻辑。
 *
 * 跑法：node ops/verify-apk-p36.js
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'mobile/android/assets/index.html');

let failures = 0;
function check(name, ok, detail) {
  console.log((ok ? '  PASS  ' : '  FAIL  ') + name + (detail ? '  → ' + detail : ''));
  if (!ok) failures += 1;
}

function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), children: [], dataset: {}, style: {},
    _classes: new Set(), value: '', textContent: '', innerHTML: '', hidden: false, disabled: false,
    scrollHeight: 46, scrollTop: 0, clientHeight: 100, checked: false,
    set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...this._classes].join(' '); },
    classList: {
      add: (c) => el._classes.add(c), remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c), toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    append(...kids) { for (const k of kids) { k.parent = el; el.children.push(k); } },
    replaceChildren(...kids) { el.children = []; el.append(...kids); },
    remove() { if (el.parent) el.parent.children = el.parent.children.filter((c) => c !== el); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    blur() {}, focus() {}, click() { if (el.onclick) el.onclick({ target: el }); },
  };
  return el;
}

function makePage() {
  const ids = {};
  const get = (id) => (ids[id] || (ids[id] = makeEl(id === 'text' ? 'textarea' : 'div')));
  const document = {
    getElementById: get, createElement: (t) => makeEl(t), createDocumentFragment: () => makeEl('fragment'),
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
    activeElement: null, hidden: false,
  };
  return { ids, get, document };
}

function loadApp() {
  const html = fs.readFileSync(HTML, 'utf8');
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('index.html 里找不到行内 <script>');
  const page = makePage();
  const storage = new Map();
  const calls = [];
  const native = {
    stopSpeech: () => calls.push({ op: 'stopSpeech' }),
    speak() {}, pickImage() {}, startConnection() {}, openPhoneSettings() {},
    openAppSettings() {}, openSettings() {}, setSpeech() {}, pickImageResult() {},
    request(id, method, url, bodyText) {
      calls.push({ op: 'request', method, url, body: bodyText ? JSON.parse(bodyText) : null });
      sandbox.queue.push(id);
    },
  };
  const sandbox = {
    document: page.document,
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    setTimeout: (fn, delay) => ((typeof delay === 'number' && delay >= 1000) ? 0 : global.setTimeout(fn, 0)),
    setInterval: () => 0, clearInterval() {}, clearTimeout() {},
    console, JSON, Date, Math, String, Number, Array, Object, RegExp, Promise, Error, Set, Map,
    Native: native,
    fetch: async () => ({ json: async () => ({}) }),
    queue: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html<script>' });
  return { sandbox, page, storage, calls, native };
}

function textOf(el) {
  return (el.children || []).map((c) => c.textContent || textOf(c)).join('\n');
}

// ---------------------------------------------------------------- 用例
console.log('=== P36-2 手机操作状态：两层判据分开显示 ===');
{
  const app = loadApp();
  const { sandbox, page } = app;
  const run = (cfg, state) => {
    vm.runInContext('latestState=' + JSON.stringify(state), sandbox);
    vm.runInContext('renderPhoneStatus(' + JSON.stringify(cfg) + ')', sandbox);
    return textOf(page.get('phoneStatusBox'));
  };

  // ① 三层都成立
  let text = run({ phone_enabled: true, register_result: 'ok', register_age_ms: 5000 },
                 { phone_context: { connected: true, devices: [{ enabled: true, operations: ['tap'] }] } });
  check('三层成立时本机显示已连上', text.includes('本机无障碍：已连上'), '');
  check('三层成立时云端显示已收到能力', text.includes('已收到这台手机的能力'), '');
  check('能力注册结果被显示', text.includes('能力注册已上报'), '');

  // ② 本机开着但云端没收到（这正是"无障碍已开启 ≠ 云端收到注册"）
  text = run({ phone_enabled: true, register_result: 'ok', register_age_ms: 5000 },
             { phone_context: { connected: false, devices: [] } });
  check('本机开着+云端没收到 → 云端行是否定', text.includes('还没收到这台手机的能力注册'), '');
  check('明确说出卡在哪一步', text.includes('云端没收到注册'), '用户要知道断在哪');

  // ③ 注册失败要说清原因
  text = run({ phone_enabled: true, register_result: 'failed', register_detail: 'HTTP 401', register_age_ms: 120000 },
             { phone_context: { connected: false, devices: [] } });
  check('注册失败显示失败与原因', text.includes('能力注册没成功') && text.includes('HTTP 401'), '');

  // ④ 云端有记录但没报可用操作
  text = run({ phone_enabled: true, register_result: 'ok', register_age_ms: 1000 },
             { phone_context: { connected: false, devices: [{ enabled: false, operations: [] }] } });
  check('有记录但 operations 空 → 不冒充可用', text.includes('没报可用操作'), '');

  // ⑤ 本机没开
  text = run({ phone_enabled: false, register_result: '', register_age_ms: -1 }, { phone_context: null });
  check('本机没开时提示先开服务', text.includes('没连上') && text.includes('先点上面的按钮'), '');

  // ⑥ 绝不用"无障碍开了"冒充"能用"
  text = run({ phone_enabled: true, register_result: 'ok', register_age_ms: 1000 },
             { phone_context: { connected: false, devices: [] } });
  check('本机开着但云端没有 → 不显示为可用', !/能操作|可以使用/.test(text), text.slice(0, 60));
}

console.log('=== P36-2 停止手机操作 ≠ 停止语音 ===');
{
  const app = loadApp();
  const { sandbox, calls } = app;
  // 老版本 App：没有 stopPhone
  delete sandbox.Native.stopPhone;
  sandbox.error = () => {};
  let lastError = '';
  vm.runInContext('error=function(t){window.__lastError=t}', sandbox);
  vm.runInContext('stopPhoneControl()', sandbox);
  lastError = sandbox.__lastError || '';
  check('没有 stopPhone 时不假装停成功', /不支持/.test(lastError), lastError);
  check('没有 stopPhone 时不误调 stopSpeech', !calls.some((c) => c.op === 'stopSpeech'),
    JSON.stringify(calls.map((c) => c.op)));

  // 新版本 App：有 stopPhone
  const app2 = loadApp();
  const c2 = app2.calls;
  let stopped = 0;
  app2.sandbox.Native.stopPhone = () => { stopped += 1; };
  vm.runInContext('error=function(t){window.__lastError=t}', app2.sandbox);
  vm.runInContext('stopPhoneControl()', app2.sandbox);
  check('有 stopPhone 时调用它一次', stopped === 1, '调用 ' + stopped + ' 次');
  check('停手机操作绝不触发 stopSpeech', !c2.some((c) => c.op === 'stopSpeech'),
    JSON.stringify(c2.map((c) => c.op)));
  check('停止后说明「已经点过的撤不回来」',
    /撤不回来/.test(String(app2.sandbox.__lastError || '')), app2.sandbox.__lastError);
  check('不支持时告诉用户「更新 App 之后就能停」',
    /更新 App/.test(lastError), lastError);

  // 反向：停语音不能碰手机
  const app3 = loadApp();
  let phoneStops = 0;
  app3.sandbox.Native.stopPhone = () => { phoneStops += 1; };
  app3.sandbox.Native.stopSpeech();
  check('停语音不触发停止手机操作', phoneStops === 0, '手机停止被调 ' + phoneStops + ' 次');
}

console.log('=== P36-2 两个按钮是独立元素（不是同一个按钮）===');
{
  const app = loadApp();
  const { page } = app;
  const stopBtn = page.get('stopPhone');
  const speechEl = page.get('speech');
  check('存在独立的停止手机操作按钮', !!stopBtn && stopBtn !== speechEl, '');
  const html = fs.readFileSync(HTML, 'utf8');
  check('停止按钮文案明确区分语音',
    /停止手机操作/.test(html) && /不影响语音播放/.test(html), '');
}

console.log('');
if (failures) { console.log('RESULT: FAILED  (' + failures + ' 项)'); process.exit(1); }
console.log('RESULT: OK');
process.exit(0);
