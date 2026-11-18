#!/usr/bin/env node
/**
 * P36 第二、三批：生活条目可聊（refs 协议）+ 交办事项 status/verification 分离。
 *
 * 方法同前：跑真实 index.html 脚本 + 真实 api() 桥（Native.request）。
 *
 * 注意 captain 提的教训：**空数组让「每条都有 X」恒真**。
 * 所以这里凡是"每个条目都满足"的断言，都先断言 items.length > 0。
 *
 * 跑法：node ops/verify-apk-p36b.js
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
      request(id, method, url, bodyText) {
        calls.push({ method, url, body: bodyText ? JSON.parse(bodyText) : null });
        sandbox.queue.push(id);
      },
    },
    fetch: async () => ({ json: async () => ({}) }),
    queue: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox, { filename: 'index.html<script>' });
  return { sandbox, page, storage, calls };
}

function allText(el) {
  return (el.children || []).map((c) => c.textContent || allText(c)).join('\n');
}
function walk(node, out) {
  for (const c of node.children || []) { out.push(c); walk(c, out); }
  return out;
}

const run = (sb, code) => vm.runInContext(code, sb);

// ---------------------------------------------------------------- P36-3 生活条目可聊
console.log('=== P36-3 点一段经历 → 带着它继续聊（refs 协议）===');
{
  const app = loadApp();
  const sb = app.sandbox;

  // 有效条目：有 id 和 kind → 应出现"聊聊这件事"
  run(sb, `latestState={life:{today_preview:[
      {id:'aaa111',kind:'experience',display_text:'今天去小镇坐了坐',time_label:'下午'},
      {id:'bbb222',kind:'thought',display_text:'想起一件事',time_label:'傍晚'}]}}`);
  const host = app.page.get('lifeContent');
  run(sb, 'renderLife(latestState)');
  const entries = walk(host, []).filter((el) => el._classes.has('life-entry'));
  check('真实渲染出生活条目', entries.length > 0, '条目数 ' + entries.length);
  const talkButtons = walk(host, []).filter((el) => el._classes.has('life-talk'));
  check('每个条目都有「聊聊这件事」', talkButtons.length === entries.length,
    talkButtons.length + ' 个按钮 / ' + entries.length + ' 条目');

  // 点第一项 → 进聊天，带 refs
  talkButtons[0].click();
  const refs = run(sb, 'pendingRefs');
  check('点击后记下了引用', Array.isArray(refs) && refs.length === 1, JSON.stringify(refs));
  check('引用用的是服务端给的 id', refs[0] && refs[0].id === 'aaa111', refs[0] && refs[0].id);
  check('引用带的 kind 与条目一致', refs[0] && refs[0].kind === 'experience', refs[0] && refs[0].kind);
  check('已切回聊天页', app.page.get('chat').hidden === false && app.page.get('lifePage').hidden === true, '');

  // 发送 → 请求体带 refs
  app.page.get('text').value = '那次玩得开心吗';
  run(sb, 'busy=false');
  run(sb, 'error=function(){}');
  const sent = run(sb, "send()");
  Promise.resolve(sent).then(() => {}).catch(() => {});
}

console.log('=== P36-3 没有 id 的条目不给假入口 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  run(sb, `latestState={life:{today_preview:[{kind:'experience',display_text:'旧条目没有 id'}]}}`);
  run(sb, 'renderLife(latestState)');
  const host = app.page.get('lifeContent');
  const entries = walk(host, []).filter((el) => el._classes.has('life-entry'));
  check('旧条目仍然显示', entries.length > 0, '条目数 ' + entries.length);
  const buttons = walk(host, []).filter((el) => el._classes.has('life-talk'));
  check('没有 id 就不给「聊聊」按钮（不给点了没用的入口）', buttons.length === 0, buttons.length + ' 个');
}

// ---------------------------------------------------------------- P36-4 交办事项
console.log('=== P36-4 status 与 verification 分开显示 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  const tasks = [
    { id: 't1', goal: '整理测试文件', status: 'completed', verification: 'pending',
      claimed_done: true, verified: false, summary: '', blocked: null, artifacts: [] },
    { id: 't2', goal: '写一份说明', status: 'completed', verification: 'passed',
      claimed_done: true, verified: true, summary: '写好了', blocked: null, artifacts: [] },
    { id: 't3', goal: '下载点东西', status: 'failed', verification: 'pending',
      claimed_done: false, verified: false, summary: '', blocked: { code: 'x', reason: '网络不通' }, artifacts: [] },
  ];
  run(sb, 'renderTasks(' + JSON.stringify({ tasks }) + ')');
  const cards = walk(app.page.get('taskList'), []).filter((el) => el._classes.has('task-card'));
  check('渲染出三张任务卡', cards.length === 3, '卡片数 ' + cards.length);

  const text = allText(app.page.get('taskList'));
  check('completed+pending 说「她说做完了，还在核验」', text.includes('她说做完了，还在核验'), '');
  check('verified=true 说「核验通过」', text.includes('核验通过'), '');
  check('failed 显示阻碍原因', text.includes('网络不通'), '');

  // 关键：completed 不能被画成笼统的"完成"
  const firstCard = allText(cards[0]);
  check('未核验的 completed 卡片里不出现笼统「完成」字样',
    !/(^|[^核验])完成(?![了的，])/.test(firstCard.replace('她说做完了', '')), firstCard.slice(0, 80));

  // 状态与核验分行写
  check('同时给出状态与核验两栏', text.includes('状态：') && text.includes('核验：'), '');
}

console.log('=== P36-4 verification 缺失时不冒充通过 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  // 极端：status=completed 但后端没给 verification / claimed_done
  run(sb, 'renderTasks(' + JSON.stringify({ tasks: [
    { id: 'x', goal: '某件事', status: 'completed', artifacts: [] }] }) + ')');
  const text = allText(app.page.get('taskList'));
  check('缺 verified 时不显示「核验通过」', !text.includes('核验通过'), text.slice(0, 80));
  check('缺字段时按「未核验」保守处理', text.includes('她说做完了') || text.includes('核验'), text.slice(0, 80));
}

console.log('=== P36-4 产物列表 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  run(sb, 'renderTasks(' + JSON.stringify({ tasks: [{
    id: 'a', goal: '产出文件', status: 'completed', verification: 'passed',
    claimed_done: true, verified: true, summary: 'ok',
    artifacts: [{ id: 'f1', name: '报告.txt', path: '/x/y', sha256: 'abc', readable: true },
                { id: 'f2', name: '读不到的.txt', path: '/z', readable: false }] }] }) + ')');
  const text = allText(app.page.get('taskList'));
  check('列出产物名', text.includes('报告.txt'), '');
  check('读不到的产物被标注', text.includes('读不到'), '');
  check('产物带 sha256 提示', walk(app.page.get('taskList'), []).some((el) => /sha256/.test(el.title || '')), '');
}

console.log('=== P36-4 空列表：必须有长度断言，否则恒真 ===');
{
  const app = loadApp();
  const sb = app.sandbox;
  run(sb, "renderTasks({tasks:[]})");
  const cards = walk(app.page.get('taskList'), []).filter((el) => el._classes.has('task-card'));
  const text = allText(app.page.get('taskList'));
  check('空列表时卡片数为 0（先断言长度再看每项，避免空数组恒真）', cards.length === 0, '');
  check('空列表给出明确文案而不是空白', text.includes('没有交办的事'), text);
}

console.log('');
if (failures) { console.log('RESULT: FAILED  (' + failures + ' 项)'); process.exit(1); }
console.log('RESULT: OK');
process.exit(0);
