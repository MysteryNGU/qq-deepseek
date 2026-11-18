#!/usr/bin/env node
/**
 * P36-5 应用更新提示。
 *
 * 契约（captain 定的）：
 * - version 为 null → **什么都不提示**
 * - 有新版 → 提示 + 下载入口
 * - 同一版本只提示一次（存已忽略版本号），且能关掉
 *
 * 跑法：node ops/verify-apk-p36c.js
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
    scrollHeight: 46, scrollTop: 0, clientHeight: 100, checked: false, title: '',
    set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...this._classes].join(' '); },
    classList: {
      add: (c) => el._classes.add(c), remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c), toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    addEventListener() {},
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

/** 装载 App；versionResponse 决定 /mobile/version 返回什么 */
function loadApp({ versionResponse = {}, nativeVersion = '0.4.6', storageSeed = {} } = {}) {
  const html = fs.readFileSync(HTML, 'utf8');
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('index.html 里找不到行内 <script>');
  const page = makePage();
  const storage = new Map(Object.entries(storageSeed));
  const calls = [];
  page.get('__nativeVersion').value = nativeVersion;
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
    Native: {
      stopSpeech() {}, speak() {}, pickImage() {}, startConnection() {}, openPhoneSettings() {},
      openAppSettings() {}, openSettings() {}, setSpeech() {}, pickImageResult() {}, stopPhone() {},
      openUrl(u) { calls.push({ op: 'openUrl', url: u }); },
      config() {
        return JSON.stringify({ version: page.get('__nativeVersion').value, server: 'https://x', paired: true });
      },
      request(id, method, url, bodyText) {
        calls.push({ op: 'request', method, url, body: bodyText ? JSON.parse(bodyText) : null });
        sandbox.queue.push(id);
      },
    },
    fetch: async () => ({ json: async () => ({}) }),
    queue: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // 让 api() 返回我们指定的 /mobile/version 响应
  sandbox.__versionResponse = versionResponse;
  vm.runInContext(m[1], sandbox, { filename: 'index.html<script>' });
  return { sandbox, page, storage, calls };
}

const tick = () => new Promise((r) => global.setTimeout(r, 5));

async function run() {
  console.log('=== P36-5 更新提示 ===');

  // 直接测纯逻辑：compareVersion
  {
    const app = loadApp();
    const cmp = (a, b) => vm.runInContext(`compareVersion(${JSON.stringify(a)},${JSON.stringify(b)})`, app.sandbox);
    check('0.4.7 > 0.4.6', cmp('0.4.7', '0.4.6') === 1);
    check('0.4.6 < 0.4.7', cmp('0.4.6', '0.4.7') === -1);
    check('0.4.6 == 0.4.6', cmp('0.4.6', '0.4.6') === 0);
    check('0.10.0 > 0.9.9（按数字比不是字符串比）', cmp('0.10.0', '0.9.9') === 1);
  }

  // 用一个可控的 api 覆盖，验证 checkUpdate 的分支
  const scenarios = [
    {
      name: 'version=null → 不提示任何东西',
      versionResponse: { version: null, reason: '读不到' },
      nativeVersion: '0.4.6',
      expectHidden: true,
      expectText: '',
    },
    {
      name: '有新版 → 提示并显示版本号',
      versionResponse: { version: '0.4.7', url: '/downloads/Xiaoyu-0.4.7.apk' },
      nativeVersion: '0.4.6',
      expectHidden: false,
      expectText: '0.4.7',
    },
    {
      name: '版本相同 → 不提示',
      versionResponse: { version: '0.4.6' },
      nativeVersion: '0.4.6',
      expectHidden: true,
      expectText: '',
    },
    {
      name: '本机更新（服务端更旧）→ 不提示',
      versionResponse: { version: '0.4.5' },
      nativeVersion: '0.4.6',
      expectHidden: true,
      expectText: '',
    },
    {
      name: '该版本已忽略过 → 不提示',
      versionResponse: { version: '0.4.7' },
      nativeVersion: '0.4.6',
      storageSeed: { 'xiaoyu.update.dismissed': '0.4.7' },
      expectHidden: true,
      expectText: '',
    },
    {
      name: '忽略的是旧版本号 → 仍要提示新版',
      versionResponse: { version: '0.4.8' },
      nativeVersion: '0.4.6',
      storageSeed: { 'xiaoyu.update.dismissed': '0.4.7' },
      expectHidden: false,
      expectText: '0.4.8',
    },
  ];

  for (const s of scenarios) {
    const app = loadApp({ versionResponse: s.versionResponse, nativeVersion: s.nativeVersion, storageSeed: s.storageSeed || {} });
    // 用桩替换 api，避免依赖真实 Native.request 往返
    app.sandbox.__resp = s.versionResponse;
    vm.runInContext('api = async function(method, url){ if(url.indexOf("/mobile/version")>=0) return window.__resp; return {}; };', app.sandbox);
    vm.runInContext('checkUpdate()', app.sandbox);
    await tick();
    const tip = app.page.get('updateTip');
    check(s.name, tip.hidden === s.expectHidden, 'hidden=' + tip.hidden);
    if (s.expectText) {
      const text = (tip.children || []).map((c) => c.textContent).join(' ');
      check('  ↑ 文案含版本号 ' + s.expectText, text.includes(s.expectText), text.slice(0, 60));
    }
  }

  console.log('=== P36-5 关闭后不再打扰 ===');
  {
    const app = loadApp({ versionResponse: { version: '0.4.7', url: '/downloads/x.apk' }, nativeVersion: '0.4.6' });
    vm.runInContext('api = async function(){ return {version:"0.4.7",url:"/downloads/x.apk"}; };', app.sandbox);
    vm.runInContext('checkUpdate()', app.sandbox);
    await tick();
    const tip = app.page.get('updateTip');
    check('先显示提示', tip.hidden === false, '');
    const kids = tip.children || [];
    const closeBtn = kids.find((c) => c._classes.has('update-close'));
    check('有可关闭按钮', !!closeBtn, '');
    if (closeBtn) closeBtn.click();
    check('点了就收起', tip.hidden === true, '');
    check('记住了已忽略的版本号', app.storage.get('xiaoyu.update.dismissed') === '0.4.7',
      String(app.storage.get('xiaoyu.update.dismissed')));
    // 再检查一次：不应再弹
    vm.runInContext('checkUpdate()', app.sandbox);
    await tick();
    check('同一版本不再弹', app.page.get('updateTip').hidden === true, '');
  }

  console.log('');
  if (failures) { console.log('RESULT: FAILED  (' + failures + ' 项)'); process.exit(1); }
  console.log('RESULT: OK');
  process.exit(0);
}

run().catch((e) => { console.error(e); process.exit(1); });
