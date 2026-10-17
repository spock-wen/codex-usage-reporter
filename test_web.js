// Run the real dashboard script with a small DOM; no browser or package dependency.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "web/index.html"), "utf8");
const script = fs.readFileSync(path.join(__dirname, "web/app.js"), "utf8");

class Element {
  constructor() {
    this.value = ""; this.children = []; this.listeners = {}; this.dataset = {};
    this.hidden = false; this.style = {}; this.attributes = {}; this.ownText = "";
    this.classes = new Set();
    this.classList = {
      toggle: (name, value) => { if (value) this.classes.add(name); else this.classes.delete(name); },
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((child) => child.textContent).join(""); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() {}
}

function boot(now, data = null) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  const chips = [...html.matchAll(/<button class="([^"]+)"[^>]*data-period="([^"]+)"[^>]*>([^<]+)<\/button>/g)].map((match) => {
    const chip = new Element(); chip.classes = new Set(match[1].split(" "));
    chip.dataset.period = match[2]; chip.textContent = match[3]; return chip;
  });
  assert.deepEqual(chips.filter((chip) => chip.classList.contains("active")).map((chip) => chip.dataset.period), ["last-week"]);
  assert.equal(chips.find((chip) => chip.dataset.period === "last7").textContent, "近 7 个自然日");
  const requests = [];
  const servicePill = new Element();
  const document = {
    getElementById(id) { assert.ok(elements.has(id), "Missing real HTML id: " + id); return elements.get(id); },
    querySelectorAll(selector) { assert.equal(selector, "[data-period]"); return chips; },
    querySelector(selector) { assert.equal(selector, ".service-pill"); return servicePill; },
    createElement() { return new Element(); },
    createElementNS() { return new Element(); },
    createTextNode(text) { const element = new Element(); element.textContent = text; return element; },
  };
  class Clock extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return new Date(now).getTime(); }
  }
  vm.runInNewContext(script, {
    document, Date: Clock, Intl, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    sessionStorage: { getItem() { return data ? "synthetic-admin" : null; } },
    location: { href: "https://codex.example.invalid/" },
    fetch: async (url) => {
      requests.push(new URL(url));
      return { status: 200, ok: true, json: async () => url.pathname.endsWith("/users") ? { users: [] } : data };
    },
  }, { filename: "web/app.js" });
  return { elements, chips, requests, click(period) { chips.find((chip) => chip.dataset.period === period).listeners.click(); } };
}

const cases = [
  ["2026-10-12T00:01:00+08:00", "2026-10-05", "2026-10-12"],
  ["2026-10-13T12:00:00+08:00", "2026-10-05", "2026-10-12"],
  ["2026-10-18T23:59:59+08:00", "2026-10-05", "2026-10-12"],
  ["2026-10-11T16:00:00Z", "2026-10-05", "2026-10-12"],
  ["2026-10-11T15:59:59Z", "2026-09-28", "2026-10-05"],
];
for (const [now, start, end] of cases) {
  test("dashboard first opens on last completed Beijing week at " + now, () => {
    const ui = boot(now);
    assert.equal(ui.elements.get("start-date").value, start);
    assert.equal(ui.elements.get("end-date").value, end);
    assert.deepEqual(ui.chips.filter((chip) => chip.classList.contains("active")).map((chip) => chip.dataset.period), ["last-week"]);
    assert.equal(ui.requests.length, 0);
  });
}

test("explicit shortcuts and manual dates remain selectable", () => {
  const ui = boot("2026-10-13T12:00:00+08:00");
  ui.click("this-week");
  assert.equal(ui.elements.get("start-date").value, "2026-10-12");
  assert.equal(ui.elements.get("end-date").value, "2026-10-14");
  ui.click("last7");
  assert.equal(ui.elements.get("start-date").value, "2026-10-07");
  assert.equal(ui.elements.get("end-date").value, "2026-10-14");
  ui.elements.get("start-date").listeners.change();
  assert.equal(ui.chips.filter((chip) => chip.classList.contains("active")).length, 0);
});

function summary(overrides = {}) {
  const totals = { requests: 1, input_tokens: 100, output_tokens: 10, total_tokens: 110,
    estimated_cost_usd: 1.25, priced_cost_usd: 1.25, unpriced_requests: 0, is_complete: true, ...overrides.totals };
  return { totals,
    by_user: [{ email: "synthetic@example.invalid", submitted: true, devices: [], totals }],
    by_day: [{ day: "2026-10-07", ...totals }],
    by_model: [{ model: "synthetic-model", ...totals }],
    conflicts: [], batches: [], pricing: {},
    ...overrides, totals,
    quality: { is_complete: true, incomplete_device_periods: 0, partial_coverage_devices: 0, missing_users: [], conflicted_events_excluded: 0, ...overrides.quality },
  };
}

async function rendered(data) {
  const ui = boot("2026-10-13T12:00:00+08:00", data);
  await new Promise((resolve) => setImmediate(resolve));
  const query = ui.requests.find((url) => url.pathname.endsWith("/summary")).searchParams;
  assert.equal(query.get("start"), "2026-10-05");
  assert.equal(query.get("end"), "2026-10-12");
  assert.equal(ui.elements.get("connection-label").textContent, "已连接");
  return ui;
}

test("complete received records show one API-equivalent cost without claiming account coverage", async () => {
  const ui = await rendered(summary());
  assert.equal(ui.elements.get("stat-cost").textContent, "$1.25");
  assert.equal(ui.elements.get("stat-cost-label").textContent, "API 等值费用");
  assert.match(ui.elements.get("notice").className, /success/);
  assert.match(ui.elements.get("notice").textContent, /本地日志不代表完整账户用量/);
  assert.equal(ui.elements.get("conflict-banner").hidden, true);
});

test("missing source coverage shows an observed cost subtotal, warning, and friendly batch reason", async () => {
  const data = summary({ totals: { estimated_cost_usd: null, is_complete: false },
    quality: { is_complete: false, incomplete_device_periods: 2 },
    batches: [{ email: "synthetic@example.invalid", device_id: "fixture-device", new: 1, duplicates: 0, conflicts: 0,
      is_complete: false, incomplete_reasons: ["source_starts_after_period"], received_at: "2026-10-13T00:00:00Z" }] });
  const ui = await rendered(data);
  assert.equal(ui.elements.get("stat-cost").textContent, "$1.25");
  assert.equal(ui.elements.get("stat-cost-label").textContent, "观测费用小计");
  assert.match(ui.elements.get("notice").className, /warning/);
  assert.match(ui.elements.get("notice").textContent, /2 个设备统计区间日志不完整/);
  assert.match(ui.elements.get("batch-list").textContent, /日志不完整.*本地日志未覆盖区间起点|本地日志未覆盖区间起点.*日志不完整/);
  assert.doesNotMatch(ui.elements.get("batch-list").textContent, /source_starts_after_period/);
  assert.doesNotMatch(ui.elements.get("model-list").textContent, /未定价/);
  assert.doesNotMatch(ui.elements.get("users-body").textContent, /未定价/);
});

test("unknown model cost remains separate from log coverage and is never double counted", async () => {
  const ui = await rendered(summary({ totals: { estimated_cost_usd: null, unpriced_requests: 1 } }));
  assert.equal(ui.elements.get("stat-cost").textContent, "$1.25");
  assert.equal(ui.elements.get("stat-cost-label").textContent, "已定价费用小计");
  assert.match(ui.elements.get("stat-cost-detail").textContent, /1 条未知模型请求未定价/);
  assert.match(ui.elements.get("notice").className, /warning/);
  assert.doesNotMatch(ui.elements.get("stat-cost-detail").textContent, /\$1.25/);
});

test("conflicts keep their own quarantine banner and partial coverage has its own warning", async () => {
  const ui = await rendered(summary({ totals: { estimated_cost_usd: null, is_complete: false },
    quality: { is_complete: false, conflicted_events_excluded: 1, partial_coverage_devices: 1 },
    conflicts: [{ reason: "content" }] }));
  assert.equal(ui.elements.get("conflict-banner").hidden, false);
  assert.match(ui.elements.get("conflict-banner").textContent, /1 条冲突记录，已隔离/);
  assert.match(ui.elements.get("notice").textContent, /1 台已上报设备仅覆盖所选区间部分日期/);
  assert.equal(ui.elements.get("stat-cost-label").textContent, "观测费用小计");
});
