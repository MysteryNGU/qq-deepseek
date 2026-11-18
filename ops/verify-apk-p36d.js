#!/usr/bin/env node
/**
 * P36-6 可用能力入口。
 *
 * 关键（captain 明确要求）：
 * - 状态词**原样显示**，不翻译 —— 所以测试不写死 "needs_setup"/"ready"，
 *   而是喂**任意**状态词，断言 UI 原样输出了它。
 * - needs_setup 与 offline 必须能区分显示（用不同的样式类，而不是同一句文案）。
 * - 数据必须来自服务端：没有该字段时明说读不到，不显示成"一切正常"。
 *
 * 跑法：node ops/verify-apk-p36d.js
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
    _classes: new Set(), value: '', _text: '', innerHTML: '', hidden: false, disabled: false,
    scrollHeight: 46, scrollTop: 0, clientHeight: 100, checked: false, title: '',
    // 真实 DOM 里 textContent 是「本节点文本 + 所有后代文本」；桩必须照做，
    // 否则 append 到已设 textContent 的元素上的文字会被漏读（这是桩的 bug，不是产品的）。
    get textContent() { return this._text + (this.children || []).map((c) => c.textContent || '').join(''); },
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); this.children = []; },
    set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get className() { return [...this._classes].join(' '); },
    classList: {
      add: (c) => el._classes.add(c), remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c), toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
    },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {}, addEventListener() {},
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
    Native: { request() {}, config() { return '{}'; } },
    fetch: async () => ({ json: async () => ({}) }),
    queue: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html<script>' });
  return { sandbox, page };
}

function walk(node, out) {
  for (const c of node.children || []) { out.push(c); walk(c, out); }
  return out;
}
const textOf = (el) => (el.children || []).map((c) => c.textContent || textOf(c)).join(' ');

console.log('=== P36-6 能力入口：状态词原样显示 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  // 故意用**任意**状态词，证明 UI 不依赖具体取值
  const rows = [
    { name: 'action.phone', status: 'ready', reason: null },
    { name: 'channel.qq', status: 'needs_setup', reason: '还没设置' },
    { name: 'channel.town', status: 'offline', reason: '连接断了' },
    { name: 'guard.watchdog', status: 'offline', reason: '这个守护停了' },
  ];
  vm.runInContext('renderCapabilities(' + JSON.stringify({ capabilities: rows }) + ')', sb);
  const host = app.page.get('capList');
  const lines = walk(host, []).filter((el) => el._classes.has('cap-line'));
  check('渲染出能力条目', lines.length > 0, lines.length + ' 条');

  const text = textOf(host);
  for (const s of ['ready', 'needs_setup', 'offline']) {
    check('状态词「' + s + '」原样出现（未被翻译）', text.includes(s), '');
  }
  check('不给 needs_setup 起别名（不显示成"未开启"之类）',
    !/未设置|未开启|待设置/.test(text), '');

  const needsLine = lines.find((l) => textOf(l).includes('needs_setup'));
  const offlineLine = lines.find((l) => textOf(l).includes('offline'));
  check('needs_setup 与 offline 样式类不同（能区分显示）',
    needsLine && offlineLine && needsLine.className !== offlineLine.className,
    (needsLine && needsLine.className) + ' vs ' + (offlineLine && offlineLine.className));

  check('原因被显示出来', text.includes('还没设置') && text.includes('连接断了'), '');
}

console.log('=== P36-6 不写死状态词：任意词都能原样显示 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  const odd = [{ name: 'x.y', status: 'some_future_state', reason: '' }];
  vm.runInContext('renderCapabilities(' + JSON.stringify({ capabilities: odd }) + ')', sb);
  const text = textOf(app.page.get('capList'));
  check('未见过的新状态词也原样输出（不会因陌生而吞掉）',
    text.includes('some_future_state'), text.slice(0, 60));
}

console.log('=== P36-6 unknown 不能画成红灯（关键）===');
{
  const app = loadApp();
  const sb = app.sandbox;
  const rows = [
    { name: 'action.web', status: 'unknown', reason: null },
    { name: 'channel.qq', status: 'needs_setup', reason: '还没设置' },
    { name: 'channel.town', status: 'offline', reason: '连接断了' },
    { name: 'action.dsh', status: 'ready', reason: null },
  ];
  vm.runInContext('renderCapabilities(' + JSON.stringify({ capabilities: rows }) + ')', sb);
  const host = app.page.get('capList');
  const lines = walk(host, []).filter((el) => el._classes.has('cap-line'));
  const byName = {};
  for (const el of lines) {
    const t = textOf(el);
    for (const r of rows) if (t.includes(r.name)) byName[r.status] = el;
  }
  check('四档都渲染出来了', Object.keys(byName).length === 4, Object.keys(byName).join(','));
  check('unknown 的样式类 ≠ offline 的样式类',
    byName.unknown && byName.offline && byName.unknown.className !== byName.offline.className,
    (byName.unknown && byName.unknown.className) + ' vs ' + (byName.offline && byName.offline.className));
  check('unknown 落成 muted（灰）而不是 bad（红）',
    byName.unknown && byName.unknown._classes.has('muted'),
    byName.unknown && byName.unknown.className);

  const unknownText = textOf(byName.unknown);
  check('unknown 那一行**不出现"不可用"**', !unknownText.includes('不可用'), unknownText);
  check('unknown 明说是状态未知', unknownText.includes('状态未知'), unknownText);
  check('needs_setup 明说"没开启"', textOf(byName.needs_setup).includes('没开启'), textOf(byName.needs_setup));
  check('offline 才说"不可用"', textOf(byName.offline).includes('不可用'), textOf(byName.offline));
  check('状态词本身仍原样显示（unknown/needs_setup/offline/ready 都在）',
    ['unknown', 'needs_setup', 'offline', 'ready'].every((w) => textOf(host).includes(w)), '');
}

console.log('=== P36-6 四档语气都有真实 CSS（防止落到默认样式而看不出来）===');
{
  const html = fs.readFileSync(HTML, 'utf8');
  for (const tone of ['ok', 'warn', 'bad', 'muted']) {
    const re = new RegExp('\\.cap-line\\.' + tone + '\\s+\\.cap-status\\s*\\{');
    check('样式类 .cap-line.' + tone + ' 有定义', re.test(html), '');
  }
}

console.log('=== P36-6 数据缺失时明说读不到 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  vm.runInContext('renderCapabilities({})', sb);
  const text = textOf(app.page.get('capList'));
  check('没有 capabilities 字段 → 明说没拿到', text.includes('没拿到'), text.slice(0, 60));
  check('绝不显示成"一切正常"', !/正常|全部可用/.test(text), text.slice(0, 60));

  const app2 = loadApp();
  vm.runInContext('renderCapabilities({capabilities:[]})', app2.sandbox);
  const text2 = textOf(app2.page.get('capList'));
  check('空清单 → 明说没有可用能力（不是空白）', text2.length > 0 && text2.includes('没有'), text2);
}

console.log('=== P36-6 后端裁剪：最多 6 条（可用 3 + 不可用 3）===');
{
  // 这里验的是**约定**：后端 patch 会裁到 6 条；前端不应依赖超过 6 条
  const app = loadApp();
  const sb = app.sandbox;
  const many = [];
  for (let i = 0; i < 5; i++) many.push({ name: 'ok' + i, status: 'ready', reason: null });
  for (let i = 0; i < 5; i++) many.push({ name: 'bad' + i, status: 'offline', reason: 'x' });
  // 后端已经裁过：只给 6 条
  const supplied = many.slice(0, 3).concat(many.slice(5, 8));
  vm.runInContext('renderCapabilities(' + JSON.stringify({ capabilities: supplied }) + ')', sb);
  const lines = walk(app.page.get('capList'), []).filter((el) => el._classes.has('cap-line'));
  check('后端按 3+3 裁剪后前端如实渲染 6 条', lines.length === 6, lines.length + ' 条');
}

console.log('');
if (failures) { console.log('RESULT: FAILED  (' + failures + ' 项)'); process.exit(1); }
console.log('RESULT: OK');
process.exit(0);
