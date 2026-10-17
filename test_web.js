// Execute the real five-page dashboard with a minimal DOM and mocked HTTP only.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'web/index.html'), 'utf8');
const script = fs.readFileSync(path.join(__dirname, 'web/app.js'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toLowerCase(); this.children = []; this.listeners = {}; this.dataset = {};
    this.attributes = {}; this.style = {}; this.hidden = false; this.disabled = false; this.open = false;
    this.ownText = ''; this.currentValue = ''; this.classes = new Set();
    this.classList = {
      toggle: (name, value) => { if (value) this.classes.add(name); else this.classes.delete(name); },
      remove: (name) => this.classes.delete(name), contains: (name) => this.classes.has(name),
    };
  }
  set className(value) { this.classes = new Set(value.split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  set value(value) { this.currentValue = String(value); }
  get value() { return this.currentValue; }
  get options() { return this.children.filter(child => child.tagName === 'option'); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.ownText = ''; this.children = children; if (this.tagName === 'select') this.currentValue = this.options[0]?.value || ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === 'class') this.className = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(selector) { return descendants(this).find(child => selector.startsWith('.') ? child.classList.contains(selector.slice(1)) : child.tagName === selector) || null; }
  focus() {}
  click() { this.listeners.click?.({ preventDefault() {} }); }
  remove() {}
}
function descendants(root) { return root.children.flatMap(child => [child, ...descendants(child)]); }
function tagged(root, cls) { return descendants(root).filter(child => child.classList.contains(cls)); }
function findTable(root) { return descendants(root).find(child => child.tagName === 'table'); }
function sortHeader(table, index) { table.children[0].children[0].children[index].children[0].click(); }
function tableBody(table) { return table.children[1].children; }
function headerLabels(table) { return table.children[0].children[0].children.map(cell => cell.textContent.replace(/[↕↑↓]/g, '').trim()); }
function attributes(text) { return Object.fromEntries([...text.matchAll(/([\w-]+)="([^"]*)"/g)].map(match => [match[1], match[2]])); }
function decode(text) { return text.replace(/<[^>]*>/g, '').replaceAll('&amp;', '&'); }
function dom() {
  const elements = new Map(), groups = new Map();
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="[^"]+"[^>]*)>/gi)) {
    const attrs = attributes(match[2]), element = new Element(match[1]); element.attributes = attrs;
    element.hidden = /\bhidden(?:\s|$)/.test(match[2]); element.className = attrs.class || '';
    elements.set(attrs.id, element);
  }
  for (const match of html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)) {
    const attrs = attributes(match[1]), element = elements.get(attrs.id) || new Element('button');
    element.className = attrs.class || ''; element.textContent = decode(match[2]);
    for (const [key, value] of Object.entries(attrs)) if (key.startsWith('data-')) {
      const name = key.slice(5); element.dataset[name] = value;
      if (!groups.has(name)) groups.set(name, []); groups.get(name).push(element);
    }
  }
  for (const match of html.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
    const select = elements.get(attributes(match[1]).id); if (!select) continue;
    select.replaceChildren(...[...match[2].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(item => {
      const option = new Element('option'); option.value = attributes(item[1]).value || ''; option.textContent = decode(item[2]); return option;
    }));
  }
  const pill = new Element(), body = new Element('body');
  return { elements, groups, document: {
    body, getElementById(id) { assert.ok(elements.has(id), 'Missing actual HTML id ' + id); return elements.get(id); },
    querySelector(selector) { assert.equal(selector, '.service-pill'); return pill; },
    querySelectorAll(selector) { const match = /^\[data-([\w-]+)\]$/.exec(selector); assert.ok(match, selector); return groups.get(match[1]) || []; },
    createElement(tag) { return new Element(tag); }, createElementNS(ns, tag) { return new Element(tag); },
    createTextNode(text) { const element = new Element('#text'); element.textContent = text; return element; },
  }};
}
const NOW = '2026-10-17T12:00:00+08:00';
const START = '2026-10-05', END = '2026-10-12';
function totals(tokens, overrides = {}) { return { requests: tokens ? 1 : 0, total_tokens: tokens, input_tokens: tokens * .9, cached_input_tokens: tokens * .5, output_tokens: tokens * .1, estimated_cost_usd: 1.25, priced_cost_usd: 1.25, unpriced_requests: 0, is_complete: true, ...overrides }; }
function codex(rows = [{ email: 'codex@example.invalid', submitted: true, totals: totals(100), devices: [] }], overrides = {}) {
  return { period_start: START + 'T00:00:00+08:00', period_end: END + 'T00:00:00+08:00', totals: totals(rows.reduce((sum, row) => sum + (row.totals?.total_tokens || 0), 0)), by_user: rows,
    by_day: [], by_model: [{ model: 'codex-model', ...totals(100) }], batches: [], conflicts: [],
    quality: { is_complete: true, incomplete_device_periods: 0, partial_coverage_devices: 0, missing_users: [], conflicted_events_excluded: 0 }, ...overrides };
}
function cursor(rows = [{ email: 'cursor@example.invalid', available: true, totals: totals(50, { usage_records: 2, charged_cents: 500, charge_complete: true }), models: [{ model: 'cursor-model', total_tokens: 50, usage_records: 2 }] }], overrides = {}) {
  return { period_start: START + 'T00:00:00+08:00', period_end: END + 'T00:00:00+08:00', totals: totals(rows.reduce((sum, row) => sum + (row.totals?.total_tokens || 0), 0), { usage_records: 2, charged_cents: 500, charge_complete: true }), by_user: rows,
    by_day: [], by_model: [{ model: 'cursor-model', total_tokens: 50 }], sync_runs: [],
    quality: { is_complete: true, account_count: rows.length, success_count: rows.length, failed_count: 0, last_success_at: '2026-10-12T10:45:00+08:00', coverage_end: '2026-10-12T00:00:00+08:00' }, ...overrides };
}
function response(data, status = 200) { return { status, ok: status >= 200 && status < 300, json: async () => structuredClone(data) }; }
function boot({ now = NOW, login = false, codexData = codex(), cursorData = cursor(), users = [], people, fetcher } = {}) {
  const { elements, groups, document } = dom(), calls = [];
  class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return new Date(now).getTime(); } }
  const localFetch = async (url, options) => {
    url = new URL(url); calls.push({ url, options });
    if (fetcher) return fetcher(url, options);
    if (url.pathname.endsWith('/cursor/summary')) return response(cursorData);
    if (url.pathname.endsWith('/summary')) return response(codexData);
    if (url.pathname.endsWith('/users')) return response({ users, ...(people ? { people } : {}) });
    throw new Error('Unexpected endpoint ' + url.pathname);
  };
  vm.runInNewContext(script, { document, Node: Element, Date: Clock, Intl, URL, URLSearchParams, AbortController, Blob, setTimeout, clearTimeout,
    location: { href: 'https://codex.example.invalid/' }, sessionStorage: { getItem() { return login ? 'synthetic-admin' : ''; }, setItem() {}, removeItem() {} }, fetch: localFetch }, { filename: 'web/app.js' });
  return { elements, groups, calls, click(kind, value) { const button = groups.get(kind)?.find(item => item.dataset[kind] === value); assert.ok(button, kind + ':' + value); button.click(); }, change(id, value, event = 'change') { elements.get(id).value = value; elements.get(id).listeners[event]?.({ preventDefault() {} }); } };
}
async function flush() { await new Promise(resolve => setImmediate(resolve)); await new Promise(resolve => setImmediate(resolve)); }

for (const [now, start, end] of [
  ['2026-10-12T00:01:00+08:00', START, END], ['2026-10-13T12:00:00+08:00', START, END],
  ['2026-10-18T23:59:59+08:00', START, END], ['2026-10-11T16:00:00Z', START, END],
  ['2026-10-11T15:59:59Z', '2026-09-28', '2026-10-05'],
]) test('default period is last completed Beijing week at ' + now, () => {
  const ui = boot({ now });
  assert.equal(ui.elements.get('start-date').value, start);
  assert.equal(ui.elements.get('end-date').value, end);
  assert.deepEqual(ui.groups.get('period').filter(button => button.classList.contains('active')).map(button => button.dataset.period), ['last-week']);
  assert.equal(ui.calls.length, 0);
});

test('four navigation pages remain reachable and the people maintenance page is hidden', () => {
  const ui = boot();
  for (const page of ['overview', 'weekly', 'members', 'sync']) {
    ui.click('page', page);
    for (const other of ['overview', 'weekly', 'members', 'sync']) assert.equal(ui.elements.get('page-' + other).hidden, other !== page);
    assert.equal(ui.elements.get('source-filter').hidden, page === 'sync');
    assert.equal(ui.elements.get('member-filters').hidden, page === 'sync');
    assert.equal(ui.elements.get('toolbar-note').hidden, page === 'sync');
    if (page === 'overview') assert.match(ui.elements.get('toolbar-note').textContent, /Top 10 占比.*分母/);
    if (page === 'weekly') assert.match(ui.elements.get('toolbar-note').textContent, /固定 Top 5 占比图不随姓名筛选变化/);
    if (page === 'members') assert.match(ui.elements.get('toolbar-note').textContent, /左侧成员及详情/);
  }
  ui.click('page', 'overview');
  assert.doesNotMatch(html, /data-page="people"|id="page-people"/);
  assert.equal(ui.calls.length, 0);
  assert.doesNotMatch(ui.elements.get('ranking-table').textContent, /120\.0M|36\.0M/);
});

test('this-week chooses a whole natural week, while rolling calendar-day range never calls Cursor with invalid bounds', async () => {
  const ui = boot({ login: true }); await flush();
  ui.click('period', 'this-week'); await flush();
  assert.equal(ui.elements.get('start-date').value, '2026-10-12');
  assert.equal(ui.elements.get('end-date').value, '2026-10-19');
  assert.match(ui.elements.get('notice').textContent, /所选周尚未结束/);
  assert.equal(ui.calls.filter(call => call.url.pathname.endsWith('/cursor/summary')).at(-1).url.searchParams.get('end'), '2026-10-19');
  const previous = ui.calls.filter(call => call.url.pathname.endsWith('/cursor/summary')).length;
  ui.click('period', 'last7'); await flush();
  assert.equal(ui.elements.get('start-date').value, '2026-10-11');
  assert.equal(ui.elements.get('end-date').value, '2026-10-18');
  assert.equal(ui.calls.filter(call => call.url.pathname.endsWith('/cursor/summary')).length, previous);
  assert.match(ui.elements.get('source-cards').textContent, /Cursor 仅支持/);
  assert.equal(ui.elements.get('connection-label').textContent, '已连接');
});

test('source selection changes visible tokens without mixing fees or request definitions', async () => {
  const ui = boot({ login: true }); await flush();
  assert.equal(ui.elements.get('access-panel').hidden, true);
  const value = () => tagged(ui.elements.get('share-chart'), 'donut-center')[0].children[0].textContent;
  assert.equal(value(), '150');
  const cards = ui.elements.get('source-cards').children;
  assert.match(cards[0].textContent, /\$1\.25/); assert.match(cards[0].textContent, /API 等值/);
  assert.match(cards[1].textContent, /\$5\.00/); assert.match(cards[1].textContent, /接口金额/);
  assert.doesNotMatch(ui.elements.get('share-chart').textContent, /\$/);
  assert.match(cards[0].textContent, /有效响应/); assert.match(cards[1].textContent, /用量记录/);
  assert.equal(ui.elements.get('share-metric').options.find(option => option.value === 'cost').disabled, true);
  ui.click('source', 'cursor'); await flush(); assert.equal(value(), '50');
  assert.equal(ui.elements.get('share-metric').options.find(option => option.value === 'cost').disabled, false);
  assert.doesNotMatch(ui.elements.get('ranking-table').textContent, /codex@example/);
  ui.click('source', 'codex'); await flush(); assert.equal(value(), '100');
  assert.doesNotMatch(ui.elements.get('ranking-table').textContent, /cursor@example/);
  ui.elements.get('disconnect-button').click();
  assert.equal(ui.elements.get('access-panel').hidden, false);
});

test('overview ranking sorts by text and underlying Token values, with accessible direction state', async () => {
  const rows = [
    { email: 'zulu@example.invalid', submitted: true, totals: totals(1200000), devices: [] },
    { email: 'beta@example.invalid', submitted: true, totals: totals(950000), devices: [] },
    { email: 'alpha@example.invalid', submitted: true, totals: totals(25), devices: [] },
  ];
  const people = [
    { id: 'z', name: 'Zulu', department: '研发', emails: [rows[0].email] },
    { id: 'b', name: 'Beta', department: '研发', emails: [rows[1].email] },
    { id: 'a', name: 'Alpha', department: '产品', emails: [rows[2].email] },
  ];
  const ui = boot({ login: true, people, codexData: codex(rows), cursorData: cursor([]) }); await flush();
  const table = findTable(ui.elements.get('ranking-table'));
  const names = () => tableBody(table).map(row => row.children[0].textContent);
  assert.deepEqual(names(), ['Zulu', 'Beta', 'Alpha']); // keep the business default before interaction
  assert.deepEqual(headerLabels(table).slice(0, 2), ['姓名 / 邮箱', '部门']);
  assert.equal(table.children[0].children[0].children[0].attributes['aria-sort'], 'none');
  sortHeader(table, 0);
  assert.deepEqual(names(), ['Alpha', 'Beta', 'Zulu']);
  assert.equal(table.children[0].children[0].children[0].attributes['aria-sort'], 'ascending');
  sortHeader(table, 4); // 25, 950K, 1.2M must compare as numbers, not formatted text.
  assert.deepEqual(names(), ['Zulu', 'Beta', 'Alpha']);
  sortHeader(table, 4);
  assert.deepEqual(names(), ['Alpha', 'Beta', 'Zulu']);
  assert.equal(table.children[0].children[0].children[4].attributes['aria-sort'], 'ascending');
});

test('Cursor account-list name and department enrich the matching Codex email without splitting usage', async () => {
  const address = 'mapped@example.invalid';
  const codexRow = { email: address, submitted: true, totals: totals(100), devices: [] };
  const cursorRow = { email: address, name: 'Cursor成员', department: '研发一部', available: true,
    totals: totals(50, { usage_records: 2, charged_cents: 500, charge_complete: true }), models: [] };
  const ui = boot({ login: true, people: [], codexData: codex([codexRow]), cursorData: cursor([cursorRow]) });
  await flush();
  const table = findTable(ui.elements.get('ranking-table'));
  assert.equal(tableBody(table).length, 1);
  assert.deepEqual(tableBody(table)[0].children.slice(0, 5).map(cell => cell.textContent), ['Cursor成员', '研发一部', '100', '50', '150']);
  assert.match(ui.elements.get('share-title').textContent, /Cursor姓名映射/);
  ui.click('page', 'members'); await flush();
  assert.match(ui.elements.get('member-list-note').textContent, /Cursor清单提供姓名和部门映射/);
  assert.match(ui.elements.get('member-detail').textContent, /Cursor账号清单映射（邮箱未验证）/);
  assert.match(ui.elements.get('member-list').textContent, /Cursor成员/);
  assert.match(ui.elements.get('member-detail').textContent, /研发一部/);
});

test('explicit people registry name and department take precedence over Cursor account metadata', async () => {
  const address = 'same@example.invalid';
  const person = { id: 'directory-person', name: '名册姓名', department: '名册部门', emails: [address] };
  const cursorRow = { email: address, name: 'Cursor姓名', department: 'Cursor部门', available: true,
    totals: totals(30, { usage_records: 1, charged_cents: 100, charge_complete: true }), models: [] };
  const ui = boot({ login: true, people: [person],
    codexData: codex([{ email: address, submitted: true, totals: totals(20), devices: [] }]),
    cursorData: cursor([cursorRow]) });
  await flush();
  const table = findTable(ui.elements.get('ranking-table'));
  assert.equal(tableBody(table).length, 1);
  assert.equal(tableBody(table)[0].children[0].textContent, '名册姓名');
  assert.equal(tableBody(table)[0].children[1].textContent, '名册部门');
  ui.click('page', 'members'); await flush();
  assert.match(ui.elements.get('member-list').textContent, /名册姓名/);
  assert.match(ui.elements.get('member-detail').textContent, /名册部门/);
  assert.match(ui.elements.get('member-detail').textContent, /人员名册映射/);
});

test('a missing source leaves the other usable instead of inventing zero data', async () => {
  const ui = boot({ login: true, fetcher: async url => url.pathname.endsWith('/cursor/summary') ? response({}, 404) : url.pathname.endsWith('/users') ? response({ users: [] }) : response(codex()) });
  await flush();
  assert.equal(ui.elements.get('connection-label').textContent, '已连接');
  assert.match(ui.elements.get('source-cards').children[0].textContent, /观测 Token100/);
  assert.match(ui.elements.get('source-cards').children[1].textContent, /尚未接入/);
  assert.match(ui.elements.get('share-scope').textContent, /部分来源缺失/);
  ui.click('source', 'cursor');
  assert.match(ui.elements.get('share-chart').textContent, /连接后/);
  assert.match(ui.elements.get('share-chart').textContent, /连接后/);
});

test('failed Cursor account with null fields remains visible and never becomes a zero-token user', async () => {
  const failed = { email: 'failed@example.invalid', available: false, error_code: 'upstream_error', totals: { requests: null, usage_records: null, total_tokens: null, charged_cents: null, charge_subtotal_cents: null, charge_complete: false, is_complete: false }, models: [] };
  const ui = boot({ login: true, codexData: codex([]), cursorData: cursor([failed], { totals: totals(0, { usage_records: 0, charged_cents: null, charge_subtotal_cents: 0, charge_complete: false, is_complete: false }), quality: { is_complete: false, account_count: 1, success_count: 0, failed_count: 1 } }) });
  await flush(); ui.click('source', 'cursor'); ui.click('page', 'members'); await flush();
  assert.match(ui.elements.get('member-list').textContent, /failed@example\.invalid/);
  assert.match(ui.elements.get('member-detail').textContent, /暂无可用记录/);
  assert.doesNotMatch(ui.elements.get('member-detail').textContent, /0 Token|0 条用量记录|\$0\.00/);
  assert.match(ui.elements.get('source-cards').textContent, /1 个账号采集失败/);
  assert.match(ui.elements.get('member-detail').textContent, /模型记录不可得/);
});

test('overview keeps the Top 10 and Other share, limits the ranking, and leaves full membership on the members page', async () => {
  const people = Array.from({ length: 11 }, (_, index) => ({ id: 'person-' + index, name: '成员' + (index + 1), department: index < 5 ? '研发' : '产品', emails: ['member' + index + '@example.invalid'] }));
  const rows = people.map(person => ({ email: person.emails[0], submitted: true, totals: totals(10), devices: [] }));
  rows.push({ email: 'unknown@example.invalid', submitted: true, totals: totals(30), devices: [] }, { email: 'shared@example.invalid', shared: true, submitted: true, totals: totals(20), devices: [] });
  const extra = { email: people[0].emails[0], available: true, totals: totals(40, { charged_cents: 500, charge_complete: true }), models: [] };
  const ui = boot({ login: true, people, codexData: codex(rows), cursorData: cursor([extra], { totals: totals(40, { charged_cents: 500, charge_complete: true }) }) });
  await flush();
  const shareRows = tagged(ui.elements.get('share-chart'), 'share-row');
  assert.equal(shareRows.length, 11); assert.match(shareRows.at(-1).textContent, /Other6030%/);
  assert.equal(tagged(ui.elements.get('share-chart'), 'donut-center')[0].textContent, '200Token');
  assert.equal(tagged(ui.elements.get('share-chart'), 'donut-slice').length, 11);
  assert.ok(tagged(ui.elements.get('share-chart'), 'donut-slice').every(slice => /Token · \d/.test(slice.attributes.title)));
  assert.match(ui.elements.get('ranking-table').textContent, /成员1/);
  assert.equal(tableBody(findTable(ui.elements.get('ranking-table'))).length, 10);
  ui.change('member-filter', '成员1', 'input');
  assert.equal(tagged(ui.elements.get('share-chart'), 'donut-center')[0].textContent, '200Token');
  assert.match(ui.elements.get('share-scope').textContent, /不随姓名或部门筛选改变/);
  ui.change('member-filter', '', 'input');
  ui.click('page', 'members'); await flush();
  assert.equal(tagged(ui.elements.get('member-list'), 'member-item').length, 13);
});

test('overview model composition shows three models per platform with hover details', async () => {
  const codexModels = [1, 2, 3, 4].map(index => ({ model: 'codex-model-' + index, total_tokens: 100 - index * 10 }));
  const cursorModels = [1, 2, 3, 4].map(index => ({ model: 'cursor-model-' + index, total_tokens: 80 - index * 10 }));
  const ui = boot({ login: true, codexData: codex(undefined, { by_model: codexModels }), cursorData: cursor(undefined, { by_model: cursorModels }) });
  await flush();
  const models = tagged(ui.elements.get('model-list'), 'model-item');
  assert.equal(models.length, 6);
  assert.equal(tagged(ui.elements.get('model-list'), 'model-source-heading').length, 2);
  const names = ui.elements.get('model-list').textContent;
  assert.match(names, /codex-model-1/); assert.match(names, /codex-model-3/); assert.doesNotMatch(names, /codex-model-4/);
  assert.match(names, /cursor-model-1/); assert.match(names, /cursor-model-3/); assert.doesNotMatch(names, /cursor-model-4/);
  assert.ok(tagged(ui.elements.get('model-list'), 'model-track').every(track => /Token · 占平台/.test(track.attributes.title)));
  assert.match(ui.elements.get('model-list').textContent, /另有 2 个模型未展示/);
});

test('member sidebar exposes accessible name, department, and Token sort controls', async () => {
  const rows = [
    { email: 'zulu@example.invalid', submitted: true, totals: totals(100), devices: [] },
    { email: 'alpha@example.invalid', submitted: true, totals: totals(20), devices: [] },
  ];
  const people = [
    { id: 'z', name: 'Zulu', department: '研发', emails: [rows[0].email] },
    { id: 'a', name: 'Alpha', department: '产品', emails: [rows[1].email] },
  ];
  const ui = boot({ login: true, people, codexData: codex(rows), cursorData: cursor([]) }); await flush();
  ui.click('page', 'members'); await flush();
  const list = ui.elements.get('member-list');
  const items = () => list.children.filter(child => child.classList.contains('member-item')).map(item => item.children[0].textContent);
  assert.deepEqual(items(), ['Zulu', 'Alpha']);
  const sortButtons = tagged(list, 'text-button');
  assert.equal(sortButtons.length, 3);
  assert.equal(sortButtons[0].attributes['aria-pressed'], 'false');
  sortButtons[0].click();
  assert.deepEqual(items(), ['Alpha', 'Zulu']);
  const refreshed = tagged(list, 'text-button');
  refreshed.find(button => button.textContent.startsWith('Token')).click();
  assert.deepEqual(items(), ['Zulu', 'Alpha']);
});

test('weekly user summary table sorts its eight-week totals', async () => {
  const rows = [
    { email: 'big@example.invalid', submitted: true, totals: totals(1000), devices: [] },
    { email: 'small@example.invalid', submitted: true, totals: totals(20), devices: [] },
  ];
  const ui = boot({ login: true, codexData: codex(rows), cursorData: cursor([]) }); await flush();
  ui.click('page', 'weekly');
  for (let attempt = 0; attempt < 6; attempt++) await flush();
  assert.equal(tagged(ui.elements.get('weekly-total'), 'week-fill').length, 8);
  assert.ok(tagged(ui.elements.get('weekly-total'), 'week-fill').every(fill => /Token：.*团队占比：/.test(fill.attributes.title)));
  assert.ok(tagged(ui.elements.get('weekly-share'), 'stack-segment').every(segment => segment.attributes.title));
  const table = findTable(ui.elements.get('weekly-share'));
  assert.ok(table, 'weekly page should expose a sortable user summary');
  assert.deepEqual(headerLabels(table), ['姓名 / 邮箱', '部门', '8 周观测 Token', '最近周 Token', '覆盖情况']);
  const names = () => tableBody(table).map(row => row.children[0].textContent);
  assert.deepEqual(names(), ['big@example.invalid', 'small@example.invalid']);
  sortHeader(table, 2);
  assert.deepEqual(names(), ['big@example.invalid', 'small@example.invalid']);
  sortHeader(table, 2);
  assert.deepEqual(names(), ['small@example.invalid', 'big@example.invalid']);
  ui.change('member-filter', 'nobody@example.invalid', 'input');
  for (let attempt = 0; attempt < 6; attempt++) await flush();
  const filteredLegend = tagged(ui.elements.get('weekly-share'), 'legend-button');
  assert.ok(filteredLegend.every(button => button.disabled));
  assert.ok(filteredLegend.every(button => /不在当前成员筛选范围/.test(button.attributes.title)));
});

test('source cost subtotals preserve incomplete charge state instead of pretending to be a complete bill', async () => {
  const ui = boot({ login: true, cursorData: cursor(undefined, { totals: totals(50, { charged_cents: null, charge_subtotal_cents: 320, charge_complete: false, is_complete: false }) }) });
  await flush();
  const card = ui.elements.get('source-cards').children[1];
  assert.match(card.textContent, /\$3\.20/); assert.match(card.textContent, /费用不完整/);
  assert.match(card.textContent, /非订阅发票或支付账单/);
});

test('weekly source switch prevents old asynchronous requests from overwriting the new selection', async () => {
  let stage = 'initial'; const pending = [];
  const ui = boot({ login: true, fetcher: async url => {
    if (url.pathname.endsWith('/users')) return response({ users: [] });
    if (stage === 'initial') return response(url.pathname.endsWith('/cursor/summary') ? cursor() : codex());
    return new Promise(resolve => pending.push({ stage, resolve, url }));
  } });
  await flush(); stage = 'old'; ui.click('page', 'weekly');
  assert.equal(pending.filter(item => item.stage === 'old').length, 4);
  stage = 'new'; ui.click('source', 'codex');
  const resolveNew = () => {
    for (const item of pending.filter(item => item.stage === 'new' && !item.done)) {
      item.done = true;
      item.resolve(response(codex([{ email: 'new@example.invalid', submitted: true, totals: totals(13), devices: [] }])));
    }
  };
  for (let index = 0; index < 4; index++) { resolveNew(); await flush(); }
  assert.equal(pending.filter(item => item.stage === 'new').length, 8);
  assert.equal(ui.elements.get('weekly-member').options.length, 1);
  assert.equal(ui.elements.get('weekly-member').options[0].textContent, 'new@example.invalid');
  for (const item of pending.filter(item => item.stage === 'old')) item.resolve(response(codex([{ email: 'old@example.invalid', submitted: true, totals: totals(999), devices: [] }])));
  await flush();
  assert.equal(ui.elements.get('weekly-member').options[0].textContent, 'new@example.invalid');
  assert.doesNotMatch(ui.elements.get('weekly-total').textContent, /999/);
  assert.equal(pending.filter(item => item.stage === 'old').length, 4);
});

test('Cursor sync records use actual captured snapshot contract rather than invented execution fields', async () => {
  const syncRuns = [
    { captured_at: '2026-10-12T10:45:00+08:00', period_start: START, period_end: END, success: 27, failed: 1, pagination_complete: false, complete: false },
    { captured_at: '2026-10-12T11:20:00+08:00', period_start: START, period_end: END, success: 28, failed: 0, pagination_complete: true, complete: true },
  ];
  const ui = boot({ login: true, cursorData: cursor(undefined, { sync_runs: syncRuns,
    quality: { is_complete: true, account_count: 28, success_count: 28, failed_count: 0, last_success_at: '2026-10-12T11:20:00+08:00', coverage_end: END + 'T00:00:00+08:00' } }) });
  await flush(); ui.click('page', 'sync'); ui.click('sync', 'cursor');
  const content = ui.elements.get('sync-content');
  assert.match(content.textContent, /最近成功快照的采集时间/);
  assert.doesNotMatch(content.textContent, /任务成功完成时间|执行成功|开始时间|结束时间|状态未提供/);
  const tableElement = descendants(content).find(element => element.tagName === 'table');
  assert.deepEqual(headerLabels(tableElement), ['采集时间', '报告自然周（不含结束）', '快照状态', '成功账号', '失败账号']);
  assert.equal(tableElement.children[0].children[0].children[0].attributes['aria-sort'], 'none');
  let rows = tableBody(tableElement);
  assert.equal(rows.length, 2);
  assert.match(rows[0].children[0].textContent, /10.12.*10:45/);
  assert.equal(rows[0].children[1].textContent, START + ' → ' + END);
  assert.equal(rows[0].children[2].textContent, '部分快照 · 分页未完成');
  assert.deepEqual(rows[0].children.slice(3).map(element => element.textContent), ['27', '1']);
  assert.equal(rows[1].children[2].textContent, '完整快照');
  assert.deepEqual(rows[1].children.slice(3).map(element => element.textContent), ['28', '0']);
  sortHeader(tableElement, 0);
  rows = tableBody(tableElement);
  assert.match(rows[0].children[0].textContent, /10.12.*11:20/); // underlying timestamps, newest first
  assert.equal(tableElement.children[0].children[0].children[0].attributes['aria-sort'], 'descending');
  sortHeader(tableElement, 0);
  assert.match(tableBody(tableElement)[0].children[0].textContent, /10.12.*10:45/);
  assert.equal(tableElement.children[0].children[0].children[0].attributes['aria-sort'], 'ascending');
});

test('Codex sync table sorts new-response counts numerically', async () => {
  const batches = [
    { received_at: '2026-10-12T10:00:00+08:00', email: 'first@example.invalid', device_id: 'device-a', period_start: START, period_end: END, received: 10, new: 2, duplicates: 8, conflicts: 0, is_complete: true },
    { received_at: '2026-10-12T11:00:00+08:00', email: 'second@example.invalid', device_id: 'device-b', period_start: START, period_end: END, received: 10, new: 10, duplicates: 0, conflicts: 0, is_complete: true },
  ];
  const ui = boot({ login: true, codexData: codex([], { batches }), cursorData: cursor([]) }); await flush();
  ui.click('page', 'sync');
  const syncTable = findTable(ui.elements.get('sync-content'));
  assert.ok(syncTable);
  assert.equal(tableBody(syncTable)[0].children[1].textContent, 'first@example.invalid');
  sortHeader(syncTable, 5);
  assert.equal(tableBody(syncTable)[0].children[1].textContent, 'second@example.invalid');
  assert.equal(syncTable.children[0].children[0].children[5].attributes['aria-sort'], 'descending');
});

test('members page keeps source account details after the separate people page is removed', async () => {
  const rows = [
    { email: 'zulu@example.invalid', submitted: true, totals: totals(20), devices: [] },
    { email: 'alpha@example.invalid', submitted: true, totals: totals(10), devices: [] },
  ];
  const ui = boot({ login: true, codexData: codex(rows), cursorData: cursor([]) }); await flush();
  ui.click('page', 'members'); await flush();
  assert.match(ui.elements.get('member-detail').textContent, /zulu@example\.invalid/);
  assert.doesNotMatch(html, /data-page="people"|id="page-people"/);
});

test('member model table sorts currency using numeric amounts rather than formatted strings', async () => {
  const account = { email: 'billing@example.invalid', available: true,
    totals: totals(100, { usage_records: 2, charged_cents: 1460, charge_complete: true }),
    models: [
      { model: 'model-small', usage_records: 1, input_tokens: 20, total_tokens: 50, charged_cents: 420 },
      { model: 'model-large', usage_records: 1, input_tokens: 30, total_tokens: 50, charged_cents: 1010 },
    ] };
  const ui = boot({ login: true, codexData: codex([]), cursorData: cursor([account], { totals: account.totals }) }); await flush();
  ui.click('source', 'cursor'); await flush(); ui.click('page', 'members'); await flush();
  const tables = descendants(ui.elements.get('member-detail')).filter(element => element.tagName === 'table');
  assert.equal(tables.length, 1, ui.elements.get('member-detail').textContent);
  const modelTable = tables.find(table => headerLabels(table).includes('来源费用小计'));
  const models = () => tableBody(modelTable).map(row => row.children[1].textContent);
  assert.equal(modelTable.children[0].children[0].children.length, 8, headerLabels(modelTable).join('|'));
  sortHeader(modelTable, 7);
  assert.deepEqual(models(), ['model-large', 'model-small']);
  assert.equal(tableBody(modelTable)[0].children[7].textContent, '$10.10');
  sortHeader(modelTable, 7);
  assert.deepEqual(models(), ['model-small', 'model-large']);
  assert.equal(modelTable.children[0].children[0].children[7].attributes['aria-sort'], 'ascending');
});

test('Cursor member detail preserves known charge subtotal when total charge is incomplete', async () => {
  const row = { email: 'subtotal@example.invalid', available: true,
    totals: totals(50, { usage_records: 2, charged_cents: null, charge_subtotal_cents: '420.000001', charge_complete: false, is_complete: false }), models: [] };
  const ui = boot({ login: true, cursorData: cursor([row], { totals: row.totals }) });
  await flush(); ui.click('source', 'cursor'); ui.click('page', 'members'); await flush();
  assert.match(ui.elements.get('source-cards').children[1].textContent, /\$4\.20/);
  assert.match(ui.elements.get('member-detail').textContent, /\$4\.20/);
  assert.doesNotMatch(ui.elements.get('member-detail').textContent, /\$0\.00/);
  assert.match(ui.elements.get('source-cards').children[1].textContent, /费用不完整/);
});
