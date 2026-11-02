const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const selected = new Set();
const drafts = new Map();
let data = { groups: [], platforms: [], warnings: [] };
let busy = false;
const key = (row) => JSON.stringify([row.platform_id, row.group_id]);

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}

function visibleRows() {
  const query = $("search").value.trim().toLowerCase();
  return data.groups.filter((r) => `${r.name} ${r.group_id} ${r.platform_id}`.toLowerCase().includes(query));
}

function batchState() {
  const eligible = visibleRows().filter((r) => r.available);
  const count = eligible.filter((r) => selected.has(key(r))).length;
  $("select-all").checked = eligible.length > 0 && count === eligible.length;
  $("select-all").indeterminate = count > 0 && count < eligible.length;
  $("select-all").disabled = busy || data.legacy_conflict || !eligible.length;
  for (const id of ["enable-selected", "disable-selected"]) $(id).disabled = busy || data.legacy_conflict || !selected.size;
}

function element(tag, className, text) {
  const el = document.createElement(tag);
  el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

function render() {
  const rows = visibleRows();
  const container = $("groups");
  container.replaceChildren();
  $("count").textContent = `${data.groups.length} 个群 · ${data.groups.filter((r) => r.enabled).length} 个允许发言`;
  if (!rows.length) container.append(element("div", "empty", data.groups.length ? "没有匹配的群聊" : "暂无群聊，点击刷新或在下方添加群号。"));
  for (const row of rows) {
    const item = element("div", "group");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(key(row));
    check.disabled = busy || data.legacy_conflict || !row.available;
    check.setAttribute("aria-label", `选择群 ${row.group_id}`);
    check.onchange = () => { check.checked ? selected.add(key(row)) : selected.delete(key(row)); batchState(); };
    const identity = element("div", "identity");
    identity.append(element("div", "name", row.name || `群 ${row.group_id}`));
    identity.append(element("div", "meta", `${row.group_id} · ${row.platform_id}`));
    const flags = [];
    if (!row.available) flags.push("平台未加载，连接后可操作");
    if (!row.native_enabled) flags.push("原生消息处理已关闭，请先在「自定义规则」恢复一次");
    if (flags.length) identity.append(element("div", "flags", flags.join("；")));
    const toggle = element("button", "switch");
    toggle.type = "button";
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-label", `群 ${row.group_id} 允许发言`);
    toggle.setAttribute("aria-checked", String(row.enabled));
    toggle.disabled = busy || data.legacy_conflict || !row.available;
    toggle.append(element("span", "", row.enabled ? "允许发言" : "已关闭"), element("span", "track"));
    toggle.onclick = () => switchGroups([row], !row.enabled);
    const heading = element("div", "group-heading");
    heading.append(check, identity, toggle);
    item.append(heading, imageControls(row));
    container.append(item);
  }
  $("refresh").disabled = busy;
  $("add-button").disabled = busy || data.legacy_conflict || !data.platforms.length;
  batchState();
}

function imageControls(row) {
  const box = element("div", "image-controls");
  const settings = drafts.get(key(row)) || { ...row.images };
  const disabled = busy || !row.available || data.legacy_conflict;
  const save = () => run(async () => {
    const result = await bridge.apiPost("groups/images", {
      platform_id: row.platform_id, group_id: row.group_id, images: settings,
    });
    drafts.delete(key(row));
    return result;
  }, `群 ${row.group_id} 生图设置已保存。`);
  for (const [field, label] of [["enabled", "允许生图"], ["caption", "生图配文"]]) {
    const button = element("button", "switch", label);
    button.type = "button";
    button.setAttribute("role", "switch");
    button.setAttribute("aria-label", `群 ${row.group_id} ${label}`);
    button.setAttribute("aria-checked", String(Boolean(settings[field])));
    button.disabled = disabled;
    button.append(element("span", "track"));
    button.onclick = () => { settings[field] = !settings[field]; drafts.set(key(row), settings); save(); };
    box.append(button);
  }
  const quotas = element("form", "quotas");
  for (const [field, title, min, max] of [
    ["per_request", "单次上限", 0, 100],
    ["window_minutes", "周期（分钟）", 1, 43200],
    ["window_limit", "周期上限", 0, 100000],
  ]) {
    const label = element("label", "", title);
    const input = document.createElement("input");
    input.type = "number";
    input.min = min; input.max = max; input.step = "1"; input.required = true;
    input.value = settings[field] ?? (field === "window_minutes" ? 60 : 0);
    input.disabled = disabled;
    input.setAttribute("aria-label", `群 ${row.group_id} ${title}`);
    input.oninput = () => { settings[field] = input.value === "" ? null : Number(input.value); drafts.set(key(row), settings); };
    label.append(input); quotas.append(label);
  }
  const button = element("button", "secondary", "保存限额");
  button.type = "submit"; button.disabled = disabled;
  quotas.append(button);
  quotas.onsubmit = (event) => { event.preventDefault(); save(); };
  box.append(quotas, element("div", "image-hint", `当前周期已用 ${row.image_used || 0} 张（含进行中）。上限 0 表示不限；关闭配文只发图片，不影响识图问答。`));
  if (!data.image_ready) box.append(element("div", "flags", "生图插件尚未接入，设置会保存；需启用兼容的生图插件。"));
  return box;
}

function accept(snapshot) {
  data = snapshot;
  const available = new Set(data.groups.filter((r) => r.available).map(key));
  for (const k of selected) if (!available.has(k)) selected.delete(k);
  const previous = $("platform").value;
  $("platform").replaceChildren(...data.platforms.map((p) => new Option(p.id, p.id)));
  if (data.platforms.some((p) => p.id === previous)) $("platform").value = previous;
}

async function run(operation, message) {
  if (busy) return;
  busy = true;
  render();
  notice("正在处理…");
  try {
    accept(await operation());
    notice([message, ...data.warnings].filter(Boolean).join("\n"), data.warnings.length > 0);
  } catch (error) {
    notice(`未能确认操作结果：${error.message}。请刷新核对。`, true);
  } finally {
    busy = false;
    render();
  }
}

async function switchGroups(rows, enabled) {
  await run(() => bridge.apiPost("groups/switch", {
    enabled, groups: rows.map((r) => ({ platform_id: r.platform_id, group_id: r.group_id })),
  }), `${rows.length} 个群已${enabled ? "允许发言" : "关闭发言"}，已保存。`);
}

$("search").oninput = render;
$("select-all").onchange = () => {
  for (const row of visibleRows().filter((r) => r.available)) {
    $("select-all").checked ? selected.add(key(row)) : selected.delete(key(row));
  }
  render();
};
$("enable-selected").onclick = () => switchGroups(data.groups.filter((r) => selected.has(key(r))), true);
$("disable-selected").onclick = () => switchGroups(data.groups.filter((r) => selected.has(key(r))), false);
$("refresh").onclick = () => run(() => bridge.apiPost("groups/refresh", {}), "群列表已刷新。");
$("add-group").onsubmit = (event) => {
  event.preventDefault();
  switchGroups([{ platform_id: $("platform").value, group_id: $("group-id").value.trim() }], false);
};

try {
  await bridge.ready();
  await run(() => bridge.apiGet("groups"), "开关状态已加载。");
} catch (error) {
  notice(`请从 AstrBot 的「群聊控制」视图打开面板。${error.message}`, true);
}
