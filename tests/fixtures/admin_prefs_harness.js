const assert = require('node:assert/strict');
const fs = require('node:fs');

function makeEl(tag) {
  const el = {
    tagName: tag.toUpperCase(), children: [], dataset: {}, attrs: {}, listeners: {},
    _html: '',
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
      toggle(c, on) { on ? this._set.add(c) : this._set.delete(c); },
    },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    dispatch(type, event) { (this.listeners[type] || []).forEach(fn => fn(event || {})); },
    querySelector(sel) { return this._query(sel, false); },
    querySelectorAll(sel) { return this._query(sel, true); },
    contains(node) { return node === this || this.children.some(c => c.contains(node)); },
    get innerHTML() { return this._html; },
    set innerHTML(value) { this._html = value; this.children = parseFragment(value, this); },
    _query(sel, all) {
      const out = [];
      const match = n => {
        if (sel.startsWith('.')) return n.classList.contains(sel.slice(1));
        if (sel.startsWith('[')) {
          const body = sel.slice(1, -1);
          const [key, raw] = body.split('=');
          if (raw === undefined) return key in n.attrs || key in n.dataset;
          return n.dataset[key.replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase())] === raw.replace(/^"|"$/g, '');
        }
        return n.tagName === sel.toUpperCase();
      };
      const walk = n => { n.children.forEach(c => { if (match(c)) out.push(c); walk(c); }); };
      walk(this);
      return all ? out : (out[0] || null);
    },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text || ''; },
    set className(v) { this.classList._set.clear(); String(v || '').split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); },
    get className() { return [...this.classList._set].join(' '); },
  };
  return el;
}

function parseFragment(html, parent) {
  // 只解析 harness 需要的标签与属性
  const nodes = [];
  const tagRe = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(html))) {
    const el = makeEl(m[1]);
    const attrRe = /([\w-]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = attrRe.exec(m[2]))) {
      if (a[1] === m[1]) continue;
      if (a[1].startsWith('data-')) el.dataset[a[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = a[2] ?? '';
      else if (a[1] === 'class') (a[2] || '').split(/\s+/).filter(Boolean).forEach(c => el.classList.add(c));
      else if (a[2] !== undefined) el.attrs[a[1]] = a[2];
    }
    nodes.push(el);
  }
  nodes.forEach(n => { n.parentNode = parent; });
  return nodes;
}

const store = new Map();
const html = makeEl('html');
const headerActions = makeEl('div');
headerActions.classList.add('header-actions');
const body = makeEl('body');
body.appendChild(headerActions);

global.localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
};
global.matchMedia = () => ({ matches: false, addEventListener() {} });
global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
global.document = {
  documentElement: html,
  readyState: 'complete',
  body,
  createElement: makeEl,
  querySelector: sel => (sel === '.header-actions' ? headerActions : null),
  addEventListener() {},
  dispatchEvent() {},
};
global.window = global;

const scriptPath = process.argv[2] || 'admin_ui.js';
eval(fs.readFileSync(scriptPath, 'utf8'));

assert.equal(html.getAttribute('data-theme'), 'light', '默认跟随系统 → light');
assert.equal(html.getAttribute('data-density'), 'comfortable');

const wrapper = headerActions.querySelector('.admin-prefs');
assert(wrapper, '外观切换器应挂载到 .header-actions');
assert.equal(headerActions.querySelectorAll('.admin-prefs').length, 1, '只挂载一次');

const pop = wrapper.querySelector('.admin-prefs-pop');
const darkBtn = wrapper.querySelector('[data-theme-value="dark"]');
assert(darkBtn);
window.AdminPrefs.setTheme('dark');
assert.equal(html.getAttribute('data-theme'), 'dark');
assert.equal(html.getAttribute('data-theme-mode'), 'dark');
assert.equal(store.get('lrp.theme'), 'dark');

window.AdminPrefs.setDensity('compact');
assert.equal(html.getAttribute('data-density'), 'compact');
assert.equal(store.get('lrp.density'), 'compact');

window.AdminPrefs.setTheme('auto');
assert.equal(html.getAttribute('data-theme'), 'light');
assert.equal(window.AdminPrefs.resolvedTheme(), 'light');

// 再挂载一次不应产生第二个切换器
window.AdminPrefs.mount();
assert.equal(headerActions.querySelectorAll('.admin-prefs').length, 1);
console.log('prefs harness OK');
