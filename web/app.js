(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const number = new Intl.NumberFormat("zh-CN");
  const money = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  let adminKey = "";
  let summary = null;
  let registeredUsers = [];
  let requestSequence = 0;
  let activeRequest = null;
  let emailTimer;
  let tokenDialogSequence = 0;
  let tokenRequestPending = false;
  try { adminKey = sessionStorage.getItem("codex_usage_admin") || ""; } catch (_) { /* Memory-only fallback. */ }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function finite(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
  function count(value) { return finite(value) ? number.format(value) : "—"; }
  function compact(value) {
    if (!finite(value)) return "—";
    for (const [base, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
      if (value >= base) return (value / base).toLocaleString("en-US", { maximumFractionDigits: value / base < 10 ? 2 : 1 }) + suffix;
    }
    return count(value);
  }
  function amount(value) { return finite(value) ? "$" + money.format(value) : "未定价"; }
  function costDisplay(totals = {}) {
    if (finite(totals.estimated_cost_usd)) return { value: amount(totals.estimated_cost_usd), label: "API 等值费用", detail: "按价格快照估算，非订阅账单", partial: false };
    const unpriced = finite(totals.unpriced_requests) ? totals.unpriced_requests : 0;
    return { value: amount(totals.priced_cost_usd), label: unpriced ? "已定价费用小计" : "观测费用小计",
      detail: unpriced ? count(unpriced) + " 条未知模型请求未定价；小计仅含已定价记录" : "仅含可用日志，非完整账户费用或订阅账单", partial: true };
  }
  function coverageNotice(data) {
    const quality = data.quality || {};
    const incompletePeriods = finite(quality.incomplete_device_periods) ? quality.incomplete_device_periods : 0;
    const missing = Array.isArray(quality.missing_users) ? quality.missing_users.length : 0;
    const partialDevices = finite(quality.partial_coverage_devices) ? quality.partial_coverage_devices : 0;
    const unpriced = finite(data.totals?.unpriced_requests) ? data.totals.unpriced_requests : 0;
    const incomplete = data.totals?.is_complete === false || quality.is_complete === false || incompletePeriods > 0 || missing > 0 || partialDevices > 0;
    let message = incomplete ? "仅显示可用日志的用量小计，不能视为完整账户用量。" : "已获取所选区间的上报记录；本地日志不代表完整账户用量。";
    if (incompletePeriods) message += " " + count(incompletePeriods) + " 个设备统计区间日志不完整，请检查缺失日志。";
    if (partialDevices) message += " " + count(partialDevices) + " 台已上报设备仅覆盖所选区间部分日期。";
    if (missing) message += " " + count(missing) + " 个邮箱本期未提交，不能视为已核实的零用量。";
    if (unpriced) message += " " + count(unpriced) + " 条未知模型请求尚未定价，费用仅显示已定价小计。";
    if (quality.conflicted_events_excluded > 0) message += " 冲突记录已隔离，不计入小计。";
    if (!data.totals?.requests) message += " 此区间尚无有效用量。";
    return { message, type: incomplete || unpriced ? "warning" : "success" };
  }
  function incompleteReasonText(reason) {
    const labels = { no_files: "没有找到本地日志", no_canonical_usage_records: "没有可去重的用量记录", source_starts_after_period: "本地日志未覆盖区间起点",
      conflicting_response_ids: "同一请求的记录存在冲突", unreadable_files: "部分日志无法读取", malformed_lines: "部分日志内容损坏",
      usage_records_without_response_id: "部分请求缺少去重标识", invalid_usage_timestamps: "部分请求时间无效", invalid_usage_records: "部分用量记录无效" };
    return labels[reason] || "部分日志或记录不可用";
  }
  function dateTime(value) {
    if (!value) return "尚未同步";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  function notice(text, type = "") { $("notice").textContent = text; $("notice").className = "notice" + (type ? " " + type : ""); }
  function updateConnection(connected) {
    $("connection-label").textContent = connected ? "已连接" : "尚未连接";
    document.querySelector(".service-pill").classList.toggle("connected", connected);
    $("disconnect-button").hidden = !adminKey;
    $("add-email-button").disabled = !connected;
  }
  function rememberKey(value) {
    adminKey = value;
    try { if (value) sessionStorage.setItem("codex_usage_admin", value); else sessionStorage.removeItem("codex_usage_admin"); } catch (_) { /* No persistence required. */ }
    $("disconnect-button").hidden = !value;
    if (!value) { $("upload-token").value = ""; if ($("add-email-dialog").open) $("add-email-dialog").close(); }
  }
  function clearData(message = "暂无数据") {
    summary = null;
    for (const id of ["stat-tokens", "stat-cost", "stat-requests", "stat-users"]) { $(id).textContent = "—"; $(id).removeAttribute("title"); }
    $("stat-tokens-detail").textContent = "输入与输出合计";
    $("stat-cost-detail").textContent = "按价格快照估算，非订阅账单";
    $("stat-cost-label").textContent = "API 等值费用";
    $("user-count").textContent = "—";
    $("trend-chart").replaceChildren(node("p", "empty-message", message));
    $("model-list").replaceChildren(node("p", "empty-message", message));
    $("batch-list").replaceChildren(node("p", "empty-message compact", message));
    const row = node("tr"); const cell = node("td", "empty-cell", message); cell.colSpan = 6; row.append(cell);
    $("users-body").replaceChildren(row);
    $("conflict-banner").hidden = true;
    $("export-button").disabled = true;
    $("last-updated").textContent = "尚未同步";
    $("pricing-note").textContent = "估算费用不代表实际订阅扣费。";
    $("trend-caption").textContent = "数据来自已接收并去重的上报记录";
  }
  function query() {
    const start = $("start-date").value;
    const end = $("end-date").value;
    if (!start || !end || start >= end) throw new Error("invalid_range");
    const params = new URLSearchParams({ start, end });
    if ($("email-filter").value.trim()) params.set("email", $("email-filter").value.trim());
    return params;
  }
  async function api(path, options = {}) {
    const headers = { Authorization: "Bearer " + adminKey, ...(options.headers || {}) };
    const response = await fetch(new URL("./api/" + path, location.href), { ...options, headers });
    if (response.status === 401 || response.status === 403) throw new Error("unauthorized");
    if (response.status === 409) throw new Error("already_exists");
    if (!response.ok) throw new Error("request_failed");
    return response;
  }
  function errorMessage(error) {
    if (error.message === "unauthorized") return "管理员密钥无效或已失效，请重新连接。";
    if (error.message === "invalid_range") return "请选择有效日期，结束日期必须晚于开始日期。";
    if (error.message === "invalid_data") return "服务返回的数据格式异常，本次结果未显示。";
    return "无法获取最新数据，请确认本地服务仍在运行后重试。";
  }
  async function loadData() {
    if (!adminKey) { notice("请先输入管理员密钥连接看板。"); $("admin-key").focus(); return; }
    let params;
    try { params = query(); } catch (error) { clearData("筛选日期无效"); notice(errorMessage(error), "error"); return; }
    const sequence = ++requestSequence;
    if (activeRequest) activeRequest.abort();
    activeRequest = new AbortController();
    clearData("正在读取最新数据…");
    notice("正在获取所选区间的统计结果…");
    $("refresh-button").disabled = true;
    try {
      const responses = await Promise.all([
        api("summary?" + params, { signal: activeRequest.signal }),
        api("users", { signal: activeRequest.signal })
      ]);
      const [data, users] = await Promise.all(responses.map((response) => response.json()));
      if (sequence !== requestSequence) return;
      if (!data || typeof data !== "object" || !data.totals || !Array.isArray(data.by_user) || !users || !Array.isArray(users.users)) throw new Error("invalid_data");
      summary = data;
      registeredUsers = users.users;
      updateConnection(true);
      $("admin-key").value = "";
      render();
      $("export-button").disabled = false;
      $("last-updated").textContent = "更新于 " + dateTime(new Date().toISOString()) + " · 北京时间";
      const coverage = coverageNotice(data);
      notice(coverage.message, coverage.type);
    } catch (error) {
      if (sequence !== requestSequence || error.name === "AbortError") return;
      clearData("本次读取失败，已清除旧结果");
      updateConnection(false);
      if (error.message === "unauthorized") { rememberKey(""); updateConnection(false); }
      notice(errorMessage(error), "error");
    } finally {
      if (sequence === requestSequence) $("refresh-button").disabled = false;
    }
  }
  function render() {
    const totals = summary.totals;
    $("stat-tokens").textContent = compact(totals.total_tokens);
    $("stat-tokens").title = count(totals.total_tokens) + " Token";
    $("stat-tokens-detail").textContent = "输入 " + compact(totals.input_tokens) + " · 输出 " + compact(totals.output_tokens);
    const cost = costDisplay(totals);
    $("stat-cost").textContent = cost.value;
    $("stat-cost-label").textContent = cost.label;
    $("stat-cost-detail").textContent = cost.detail;
    $("stat-requests").textContent = count(totals.requests);
    $("stat-users").textContent = count(summary.by_user.filter((row) => row.totals && row.totals.requests > 0).length);
    const conflicts = Array.isArray(summary.conflicts) ? summary.conflicts : [];
    const conflictCount = finite(summary.quality?.conflicted_events_excluded) ? summary.quality.conflicted_events_excluded : conflicts.length;
    $("conflict-banner").hidden = !conflictCount;
    if (conflictCount) {
      $("conflict-banner").textContent = "发现 " + count(conflictCount) + " 条冲突记录，已隔离并排除于有效用量。费用为已定价记录的小计，完整估算暂不可得。";
      $("conflict-banner").hidden = false;
    }
    const pricing = summary.pricing || {};
    const verified = pricing.verified_at || (pricing.snapshot && pricing.snapshot.verified_at);
    $("pricing-note").textContent = verified ? "价格快照：" + String(verified) + " · Standard API 等值，非订阅账单。" : "使用服务端价格快照估算，非订阅实际扣费。";
    const options = registeredUsers.map((user) => { const option = node("option"); option.value = String(user.email || ""); return option; });
    $("email-options").replaceChildren(...options);
    renderUsers();
    renderTrend();
    renderModels();
    renderBatches();
  }
  function allUsers() {
    const map = new Map();
    for (const user of registeredUsers) if (user.email) map.set(String(user.email), { email: String(user.email), submitted: false, devices: Array.isArray(user.devices) ? user.devices : [], totals: { requests: 0, total_tokens: 0, estimated_cost_usd: null, priced_cost_usd: 0 } });
    for (const user of summary.by_user) if (user.email) map.set(String(user.email), user);
    const filter = $("email-filter").value.trim().toLowerCase();
    const search = $("user-search").value.trim().toLowerCase();
    return [...map.values()].filter((user) => (!filter || String(user.email).toLowerCase() === filter) && (!search || String(user.email).toLowerCase().includes(search))).sort((a, b) => (b.totals?.total_tokens || 0) - (a.totals?.total_tokens || 0) || String(a.email).localeCompare(String(b.email)));
  }
  function renderUsers() {
    if (!summary) return;
    const users = allUsers();
    $("user-count").textContent = count(users.length);
    if (!users.length) {
      const row = node("tr"); const cell = node("td", "empty-cell", "暂无匹配邮箱。成员通过本机 skill 自动接入后提交用量记录。"); cell.colSpan = 6; row.append(cell); $("users-body").replaceChildren(row); return;
    }
    const rows = users.map((user) => {
      const row = node("tr"); const emailCell = node("td");
      const button = node("button", "email-button"); button.type = "button";
      const avatar = node("span", "email-avatar", String(user.email).slice(0, 1).toUpperCase()); avatar.setAttribute("aria-hidden", "true");
      button.append(avatar, document.createTextNode(String(user.email)));
      button.setAttribute("aria-label", "筛选邮箱 " + String(user.email));
      button.addEventListener("click", () => { $("email-filter").value = String(user.email); loadData(); });
      emailCell.append(button, node("span", "submission-badge" + (user.submitted ? " submitted" : " missing"), user.submitted ? "本期已提交" : "本期未提交")); row.append(emailCell);
      const devices = Array.isArray(user.devices) ? user.devices : [];
      const totals = user.totals || {};
      row.append(node("td", "numeric", count(devices.length)), node("td", "numeric", count(totals.requests)));
      const tokenCell = node("td", "numeric", compact(totals.total_tokens)); tokenCell.title = count(totals.total_tokens); row.append(tokenCell);
      const cost = costDisplay(totals);
      const costCell = node("td", "numeric" + (cost.partial ? " cost-unpriced" : ""), cost.value);
      if (cost.partial) costCell.append(node("span", "cost-subtotal", cost.label + " · " + cost.detail));
      row.append(costCell);
      const latest = [user.last_seen, user.last_sync, ...devices.map((device) => device.last_seen)].filter(Boolean).sort().pop();
      const sync = node("td", "", dateTime(latest)); if (latest) sync.title = String(latest); if (!user.submitted && latest) sync.append(node("span", "sync-history-label", "历史同步，非本期提交")); row.append(sync);
      return row;
    });
    $("users-body").replaceChildren(...rows);
  }
  function renderTrend() {
    const days = (Array.isArray(summary.by_day) ? summary.by_day : []).filter((row) => typeof (row.day || row.date) === "string" && finite(row.total_tokens)).sort((a, b) => String(a.day || a.date).localeCompare(String(b.day || b.date)));
    if (!days.length) { $("trend-chart").replaceChildren(node("p", "empty-message", "所选区间暂无每日用量")); return; }
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 740 210"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "每日输入与输出 Token 趋势，日期以北京时间计");
    const element = (tag, attrs, text) => { const el = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value)); if (text !== undefined) el.textContent = String(text); return el; };
    const left = 48, top = 14, width = 675, height = 151;
    const maximum = Math.max(1, ...days.map((day) => day.total_tokens));
    const x = (index) => left + (days.length === 1 ? width / 2 : index / (days.length - 1) * width);
    const y = (value) => top + height - value / maximum * height;
    for (let index = 0; index <= 3; index++) {
      const value = maximum * index / 3, yy = y(value);
      svg.append(element("line", { x1: left, x2: left + width, y1: yy, y2: yy, stroke: "#edf0f2", "stroke-dasharray": index ? "3 5" : "none" }));
      svg.append(element("text", { x: left - 10, y: yy + 3, "text-anchor": "end", fill: "#a1aab4", "font-size": 9 }, compact(value)));
    }
    const points = days.map((day, index) => x(index) + "," + y(day.total_tokens)).join(" ");
    svg.append(element("polygon", { points: left + "," + (top + height) + " " + points + " " + (left + width) + "," + (top + height), fill: "#f0f5fd" }));
    svg.append(element("polyline", { points, fill: "none", stroke: "#7b9fdc", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const step = Math.max(1, Math.ceil(days.length / 9));
    days.forEach((day, index) => {
      const circle = element("circle", { cx: x(index), cy: y(day.total_tokens), r: 3, fill: "#fff", stroke: "#7b9fdc", "stroke-width": 1.5 });
      circle.append(element("title", {}, String(day.day || day.date) + "：" + count(day.total_tokens) + " Token")); svg.append(circle);
      if (index % step === 0 || index === days.length - 1) svg.append(element("text", { x: x(index), y: 189, "text-anchor": "middle", fill: "#9aa5b0", "font-size": 9 }, String(day.day || day.date).slice(5).replace("-", "/")));
    });
    $("trend-chart").replaceChildren(svg);
    $("trend-caption").textContent = days.length + " 个有使用记录的日期 · K = 千 / M = 百万 / B = 十亿";
  }
  function renderModels() {
    const models = (Array.isArray(summary.by_model) ? summary.by_model : []).slice().sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0));
    if (!models.length) { $("model-list").replaceChildren(node("p", "empty-message", "所选区间暂无模型记录")); return; }
    const total = models.reduce((sum, model) => sum + (finite(model.total_tokens) ? model.total_tokens : 0), 0);
    const colors = ["#7f9fda", "#9fb8d1", "#b1c9c5", "#c0bfd4", "#cdd5dd"];
    $("model-list").replaceChildren(...models.map((model, index) => {
      const item = node("div", "model-item"), row = node("div", "model-row");
      row.append(node("span", "model-name", model.model || "unknown"), node("span", "model-number", compact(model.total_tokens) + " · " + (total ? Math.round(model.total_tokens / total * 100) : 0) + "%"));
      const track = node("div", "model-track"), fill = node("div", "model-fill");
      fill.style.width = (total && finite(model.total_tokens) ? Math.max(0, Math.min(100, model.total_tokens / total * 100)) : 0) + "%";
      fill.style.backgroundColor = colors[index % colors.length]; track.append(fill);
      const cost = costDisplay(model);
      item.append(row, track, node("p", "model-cost", cost.value + " · " + (cost.partial ? cost.label + " · " + cost.detail : "API 等值")));
      return item;
    }));
  }
  function renderBatches() {
    const batches = (Array.isArray(summary.batches) ? summary.batches : []).slice(0, 12);
    if (!batches.length) { $("batch-list").replaceChildren(node("p", "empty-message compact", "暂无上报记录。成员可通过本机 skill 自动接入并提交第一份记录。")); return; }
    $("batch-list").replaceChildren(...batches.map((batch) => {
      const item = node("div", "batch-item"), identity = node("div");
      identity.append(node("p", "batch-email", batch.email || "未知邮箱"), node("p", "batch-device", "设备 " + String(batch.device_id || "未知")));
      const badges = node("div", "batch-counts");
      badges.append(node("span", "batch-badge", "新增 " + count(batch.new)), node("span", "batch-badge duplicate", "重复 " + count(batch.duplicates)), node("span", "batch-badge conflict", "冲突 " + count(batch.conflicts)));
      if (batch.rejected > 0) badges.append(node("span", "batch-badge conflict", "拒绝 " + count(batch.rejected)));
      if (batch.is_complete === false) {
        badges.append(node("span", "batch-badge conflict", "日志不完整"));
        const reasons = Array.isArray(batch.incomplete_reasons) ? [...new Set(batch.incomplete_reasons.map(incompleteReasonText))] : [];
        identity.append(node("p", "batch-device", reasons.length ? reasons.join("；") : "部分日志或记录不可用"));
      }
      item.append(identity, badges, node("time", "batch-time", dateTime(batch.received_at)));
      return item;
    }));
  }
  function beijingToday() { return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10); }
  function addDays(value, days) { const date = new Date(value + "T00:00:00Z"); date.setUTCDate(date.getUTCDate() + days); return date.toISOString().slice(0, 10); }
  function setPeriod(period) {
    const today = beijingToday();
    const weekday = new Date(today + "T00:00:00Z").getUTCDay();
    const monday = addDays(today, -(weekday + 6) % 7);
    $("start-date").value = period === "last7" ? addDays(today, -6) : period === "this-week" ? monday : addDays(monday, -7);
    $("end-date").value = period === "last-week" ? monday : addDays(today, 1);
    document.querySelectorAll("[data-period]").forEach((button) => button.classList.toggle("active", button.dataset.period === period));
  }
  $("access-form").addEventListener("submit", (event) => { event.preventDefault(); rememberKey($("admin-key").value.trim()); loadData(); });
  $("disconnect-button").addEventListener("click", () => { if (tokenRequestPending) return; ++requestSequence; if (activeRequest) activeRequest.abort(); rememberKey(""); registeredUsers = []; $("email-options").replaceChildren(); $("admin-key").value = ""; updateConnection(false); clearData("连接后显示真实上报数据"); $("refresh-button").disabled = false; notice("已断开连接，并清除本标签页保存的管理员密钥。"); });
  $("refresh-button").addEventListener("click", loadData);
  document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => { setPeriod(button.dataset.period); if (adminKey) loadData(); }));
  for (const id of ["start-date", "end-date"]) $(id).addEventListener("change", () => { document.querySelectorAll("[data-period]").forEach((button) => button.classList.remove("active")); if (adminKey) loadData(); });
  $("email-filter").addEventListener("input", () => { clearTimeout(emailTimer); emailTimer = setTimeout(() => { if (adminKey) loadData(); }, 400); });
  $("user-search").addEventListener("input", renderUsers);
  $("export-button").addEventListener("click", async () => {
    $("export-button").disabled = true;
    try {
      const params = query(), response = await api("export.csv?" + params);
      const blob = await response.blob(), url = URL.createObjectURL(blob), anchor = node("a");
      anchor.href = url; anchor.download = "codex-usage-" + $("start-date").value + "-" + $("end-date").value + ".csv";
      document.body.append(anchor); anchor.click(); anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { if (error.message === "unauthorized") { rememberKey(""); updateConnection(false); clearData("登录已失效"); } notice(errorMessage(error), "error"); }
    finally { $("export-button").disabled = !summary; }
  });
  const dialog = $("add-email-dialog");
  $("add-email-button").addEventListener("click", () => { ++tokenDialogSequence; $("new-email").value = ""; $("upload-token").value = ""; $("email-form-status").textContent = ""; $("token-result").hidden = true; $("create-token-button").disabled = false; dialog.showModal(); $("new-email").focus(); });
  $("close-email-dialog").addEventListener("click", () => { if (!tokenRequestPending) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (tokenRequestPending) event.preventDefault(); });
  dialog.addEventListener("close", () => { ++tokenDialogSequence; $("upload-token").value = ""; $("token-result").hidden = true; });
  $("add-email-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (tokenRequestPending) return;
    tokenRequestPending = true;
    $("create-token-button").disabled = true; $("close-email-dialog").disabled = true; $("disconnect-button").disabled = true;
    $("email-form-status").textContent = "正在生成…完成前请保持此窗口打开，上传令牌仅返回一次。";
    const dialogSequence = tokenDialogSequence;
    try {
      const response = await api("users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: $("new-email").value.trim() }) });
      const data = await response.json();
      if (typeof data.upload_token !== "string" || !data.upload_token) throw new Error("invalid_data");
      if (!dialog.open || dialogSequence !== tokenDialogSequence) return;
      $("upload-token").value = data.upload_token; $("token-result").hidden = false; $("email-form-status").textContent = "邮箱已添加，上传令牌已生成。";
      $("copy-token-button").textContent = "复制上传令牌";
      loadData();
    } catch (error) { $("email-form-status").textContent = error.message === "already_exists" ? "该邮箱已存在；原上传令牌保持有效。" : errorMessage(error); $("create-token-button").disabled = false; if (error.message === "unauthorized") { rememberKey(""); updateConnection(false); clearData("登录已失效"); } }
    finally { tokenRequestPending = false; $("close-email-dialog").disabled = false; $("disconnect-button").disabled = false; }
  });
  $("copy-token-button").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("upload-token").value); $("copy-token-button").textContent = "已复制"; } catch (_) { $("upload-token").focus(); $("upload-token").select(); $("copy-token-button").textContent = "请按 Ctrl/Cmd + C 复制"; } });
  setPeriod("last-week");
  updateConnection(false);
  if (adminKey) loadData();
})();
