const state = { ws: null, connectPromise: null, wsGeneration: 0, conversationId: null, seq: 0, assistantNode: null, assistantContent: "", statusNode: null, configItems: [], skills: [], skillsLoaded: false, skillPage: 1, skillPageSize: 25, skillTotal: 0, tools: [], toolsLoaded: false, toolPage: 1, toolPageSize: 25, toolTotal: 0, swaggerOperations: [], editingSkillId: null, editingToolName: null, skillDialogMode: "edit", selectedSkillTools: [], attachment: null, umcToken: "", umcUserId: "", umcTokenPromise: null, testCases: [], testResults: [], auditConversations: [], auditScope: "owner", auditLoaded: false, auditConversationPage: 1, auditConversationPageSize: 25, auditConversationTotal: 0, auditConversationId: null, auditItems: [], auditRecordPage: 1, auditRecordPageSize: 25, auditRecordTotal: 0, auditRecordHasMore: false, auditRecordLoading: false, auditRecordRequestId: 0, consoleAuthenticated: false };
const $ = (id) => document.getElementById(id);

function debounce(callback, delay = 250) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => callback(...args), delay);
  };
}

function renderPager(id, { page, pageSize, total, onPage }) {
  const container = $(id);
  if (!container) return;
  container.replaceChildren();
  if (total <= pageSize) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const previous = document.createElement("button");
  previous.type = "button";
  previous.className = "list-pager-button";
  previous.textContent = "<";
  previous.title = "上一页";
  previous.setAttribute("aria-label", "上一页");
  previous.disabled = page <= 1;
  previous.addEventListener("click", () => onPage(page - 1));
  const label = document.createElement("span");
  label.textContent = `第 ${page} / ${totalPages} 页 · ${total} 条`;
  const next = document.createElement("button");
  next.type = "button";
  next.className = "list-pager-button";
  next.textContent = ">";
  next.title = "下一页";
  next.setAttribute("aria-label", "下一页");
  next.disabled = page >= totalPages;
  next.addEventListener("click", () => onPage(page + 1));
  container.append(previous, label, next);
}

function createClientMessageId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function containsArabic(text) {
  return /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]/u.test(text);
}

const allowedExternalMessageHosts = ["umc-customerportal.sol.daypop.ai"];

function isIpLiteral(hostname) {
  return hostname.startsWith("[")
    || hostname.endsWith("]")
    || /^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname);
}

function hostMatchesPolicy(hostname) {
  const normalizedHostname = hostname.toLowerCase().replace(/\.$/, "");
  return allowedExternalMessageHosts.some((entry) => {
    const allowed = entry.trim().toLowerCase().replace(/\.$/, "");
    if (!allowed) return false;
    if (allowed.startsWith("*.")) {
      const suffix = allowed.slice(2);
      return Boolean(suffix) && normalizedHostname.endsWith("." + suffix);
    }
    return normalizedHostname === allowed;
  });
}

function normalizeMessageHref(value) {
  const candidate = String(value || "").trim();
  if (!candidate || candidate.includes("\\")) return null;
  try {
    if (candidate.startsWith("/") && !candidate.startsWith("//")) {
      const url = new URL(candidate, window.location.origin);
      if (url.origin !== window.location.origin || url.username || url.password) return null;
      return url.pathname + url.search + url.hash;
    }

    const url = new URL(candidate);
    if (url.origin === window.location.origin && !url.username && !url.password) {
      return url.pathname + url.search + url.hash;
    }
    if (
      url.protocol !== "https:"
      || url.username
      || url.password
      || (url.port && url.port !== "443")
      || !url.hostname
      || isIpLiteral(url.hostname)
      || !hostMatchesPolicy(url.hostname)
    ) {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

function renderMarkdownLinks(node, text) {
  const linkPattern = /\[([^\]\n]+)\]\(([^)\s]+)\)/g;
  let cursor = 0;
  let match;
  while ((match = linkPattern.exec(text)) !== null) {
    node.append(document.createTextNode(text.slice(cursor, match.index)));
    const href = normalizeMessageHref(match[2]);
    if (href) {
      const link = document.createElement("a");
      link.textContent = match[1];
      link.href = href;
      link.className = "message-link";
      if (href.startsWith("https://")) {
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      }
      node.append(link);
    } else {
      node.append(document.createTextNode(match[0]));
    }
    cursor = match.index + match[0].length;
  }
  node.append(document.createTextNode(text.slice(cursor)));
}

function renderLocalizedContent(node, content) {
  node.replaceChildren();
  const text = String(content || "");
  if (!text) return;
  text.split(/\n{2,}/).forEach((paragraph) => {
    const block = document.createElement("div");
    block.className = "localized-block";
    block.dir = containsArabic(paragraph) ? "rtl" : "ltr";
    renderMarkdownLinks(block, paragraph);
    node.appendChild(block);
  });
}

function setConnection(text, online) {
  const node = $("connection");
  node.textContent = text;
  node.className = `pill ${online ? "online" : "offline"}`;
}

function addEvent(type, content, meta = "") {
  const empty = document.querySelector(".empty");
  if (empty) empty.remove();
  const row = document.createElement("article");
  row.className = `event ${type.includes("assistant") ? "assistant" : type.includes("user") ? "user" : "system"}`;
  row.innerHTML = `<div class="event-meta"><span>${type}</span><small>${meta}</small></div><div class="event-body"></div>`;
  renderLocalizedContent(row.querySelector(".event-body"), content);
  $("events").appendChild(row);
  $("events").scrollTop = $("events").scrollHeight;
  return row.querySelector(".event-body");
}

function clearAssistantStatus() {
  const row = state.statusNode?.closest(".event");
  if (row) row.remove();
  state.statusNode = null;
}

function showAssistantStatus(data, meta = "") {
  if (!state.statusNode) {
    state.statusNode = addEvent("assistant.status", "", meta);
    const row = state.statusNode.closest(".event");
    row.classList.add("status-event");
    row.setAttribute("role", "status");
    row.setAttribute("aria-live", "polite");
  }
  const row = state.statusNode.closest(".event");
  renderLocalizedContent(state.statusNode, data.message || data.content || "Working…");
  const metaNode = row.querySelector(".event-meta small");
  if (metaNode) metaNode.textContent = [data.phase || "working", meta].filter(Boolean).join(" · ");
  // Keep the live progress card at the end of the timeline so the newest
  // waiting state remains visible below any route/tool events.
  $("events").appendChild(row);
  $("events").scrollTop = $("events").scrollHeight;
}

async function api(path, options = {}) {
  const rawToken = state.umcToken || $("umcToken")?.value.trim() || "";
  const headers = { "Content-Type": "application/json", "X-User-Id": $("userId").value, "X-Tenant-Id": $("tenantId").value, ...(options.headers || {}) };
  if (rawToken && !headers.Authorization) headers.Authorization = rawToken.toLowerCase().startsWith("bearer ") ? rawToken : `Bearer ${rawToken}`;
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  if (response.status === 401 && !path.startsWith("/api/v1/console/")) {
    state.consoleAuthenticated = false;
    showConsoleGate("控制台会话已过期，请重新输入密码。", true);
  }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function consoleApi(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  if (response.status === 401) {
    state.consoleAuthenticated = false;
    showConsoleGate("控制台会话已过期，请重新输入密码。", true);
  }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function showConsoleGate(message = "请输入测试控制台密码。", focus = false) {
  const gate = $("consoleGate");
  if (!gate) return;
  gate.hidden = false;
  const status = $("consoleAuthStatus");
  if (status) status.textContent = message;
  if (focus) setTimeout(() => $("consolePassword")?.focus(), 0);
}

function hideConsoleGate() {
  const gate = $("consoleGate");
  if (gate) gate.hidden = true;
  const status = $("consoleAuthStatus");
  if (status) status.textContent = "";
  const input = $("consolePassword");
  if (input) input.value = "";
}

async function checkConsoleSession() {
  const response = await fetch("/api/v1/console/session", { credentials: "same-origin" });
  if (!response.ok) return false;
  const data = await response.json();
  return data.authenticated === true;
}

async function loginConsole(event) {
  event.preventDefault();
  const password = $("consolePassword").value;
  const status = $("consoleAuthStatus");
  status.textContent = "正在验证…";
  try {
    const response = await fetch("/api/v1/console/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!response.ok) throw new Error("密码不正确或服务不可用");
    state.consoleAuthenticated = true;
    hideConsoleGate();
    await loadUmcToken().catch(() => {});
  } catch (error) {
    status.textContent = error.message;
    $("consolePassword").select();
  }
}

function tokenClaims(rawToken) {
  try {
    const segment = String(rawToken || "").split(".")[1];
    if (!segment) return {};
    const normalized = segment.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(segment.length / 4) * 4, "=");
    const bytes = Uint8Array.from(atob(normalized), (value) => value.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    return {};
  }
}

function syncUmcIdentity(rawToken, session = {}) {
  const claims = tokenClaims(rawToken);
  const candidate = session.userId || session.userID || session.user_id || claims.UserID || claims.UserId || claims.userId || claims.userID || claims.sub;
  const userId = String(candidate || "").trim();
  if (!userId) return;
  state.umcUserId = userId;
  // The REST API and browser WebSocket must use the same validated UMC
  // identity.  Keep the field visible for diagnostics, but remove the old
  // demo-user placeholder automatically after service-account login.
  const input = $("userId");
  if (input && (!input.value.trim() || input.value.trim() === "demo-user" || input.dataset.umcAuto === "1")) {
    input.value = userId;
    input.dataset.umcAuto = "1";
  }
}

async function logoutConsole() {
  await fetch("/api/v1/console/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  state.consoleAuthenticated = false;
  state.umcToken = "";
  $("umcToken").value = "";
  setUmcSessionStatus("未登录", false);
  if (state.ws && state.ws.readyState <= 1) state.ws.close();
  setConnection("未连接", false);
  showConsoleGate("已退出测试控制台。", true);
}

async function bootstrapConsole() {
  try {
    state.consoleAuthenticated = await checkConsoleSession();
  } catch {
    state.consoleAuthenticated = false;
  }
  if (!state.consoleAuthenticated) {
    showConsoleGate("请输入测试控制台密码。", true);
    return;
  }
  hideConsoleGate();
  await loadUmcToken().catch(() => {});
}

function setUmcSessionStatus(text, online = false) {
  const field = $("umcSessionStatus");
  if (!field) return;
  field.value = text;
  field.classList.toggle("session-ready", online);
  field.classList.toggle("session-error", !online && /失败|未配置|无法|要求/.test(text));
}

async function loadUmcToken(force = false) {
  if (!force && state.umcToken) {
    syncUmcIdentity(state.umcToken);
    return { token: state.umcToken };
  }
  if (!state.umcTokenPromise || force) {
    state.umcTokenPromise = (async () => {
      setUmcSessionStatus("正在自动获取…");
      try {
        const data = await api(`/api/v1/umc/session${force ? "?refresh=true" : ""}`, { method: "POST", body: "{}" });
        const token = String(data.token || "").trim();
        if (!token) throw new Error("UMC 登录响应未返回 Token");
        state.umcToken = token;
        syncUmcIdentity(token, data);
        // Keep this only in the current page memory. The hidden field exists
        // solely for compatibility with the request header helper.
        $("umcToken").value = token;
        const minutes = Number(data.expiresInMinutes || 0);
        const suffix = minutes > 0 ? `（约 ${minutes} 分钟有效）` : "";
        setUmcSessionStatus(`已自动登录：${data.account || "UMC 账号"}${suffix}`, true);
        return data;
      } catch (error) {
        state.umcToken = "";
        $("umcToken").value = "";
        setUmcSessionStatus(`自动登录失败：${error.message}`);
        throw error;
      }
    })();
  }
  try {
    return await state.umcTokenPromise;
  } finally {
    state.umcTokenPromise = null;
  }
}

function setTab(tabId) {
  document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tabId));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabId));
  if (tabId === "configPanel" && !state.configItems.length) loadConfig();
  if (tabId === "skillPanel" && !state.skillsLoaded) loadSkills();
  if (tabId === "toolPanel" && !state.toolsLoaded) loadTools();
  if (tabId === "auditPanel" && !state.auditLoaded) loadAuditConversations();
}

function configValue(item) {
  if (item.secret) return "";
  return item.value ?? "";
}

function renderConfig(items) {
  state.configItems = items;
  const form = $("configForm");
  form.innerHTML = "";
  const groups = new Map();
  items.forEach((item) => {
    if (!groups.has(item.group)) groups.set(item.group, []);
    groups.get(item.group).push(item);
  });
  groups.forEach((groupItems, groupName) => {
    const group = document.createElement("section");
    group.className = "config-group card";
    group.innerHTML = `<h3>${groupName}</h3><div class="config-fields"></div>`;
    const fields = group.querySelector(".config-fields");
    groupItems.forEach((item) => {
      const label = document.createElement("label");
      label.className = "config-field";
      const options = Array.isArray(item.options) ? item.options : [];
      const inputType = item.secret ? "password" : /timeout|seconds|days|top_k/.test(item.key) ? "number" : "text";
      const configured = item.configured ? "已配置" : "未配置";
      label.innerHTML = `<span>${item.label}<small>${item.env || ""} · ${configured}${item.restartRequired ? " · 重启生效" : " · 可热更新"}</small></span>`;
      const input = item.multiline ? document.createElement("textarea") : options.length ? document.createElement("select") : document.createElement("input");
      if (options.length) {
        options.forEach((option) => {
          const optionNode = document.createElement("option");
          optionNode.value = typeof option === "object" ? option.value : option;
          optionNode.textContent = typeof option === "object" ? option.label : option;
          input.appendChild(optionNode);
        });
      } else if (!item.multiline) {
        input.type = inputType;
      }
      input.dataset.key = item.key;
      input.disabled = Boolean(item.readOnly);
      if (item.source === "local_override") label.querySelector("small").append(" · 本地连接，只读");
      input.dataset.secret = item.secret ? "1" : "0";
      input.dataset.multiline = item.multiline ? "1" : "0";
      input.value = configValue(item);
      input.placeholder = item.secret ? (item.configured ? "已配置，留空表示保持不变" : "输入后保存（仅保存哈希前的密文）") : "使用当前环境/默认值";
      if (item.multiline) {
        input.rows = 8;
        input.placeholder = item.description || "输入要追加到每轮系统提示词的全局指令";
      }
      if (inputType === "number") input.step = item.key.includes("timeout") ? "1" : "1";
      if (item.key === "audit_retention_days") input.min = "1";
      if (item.key === "audit_cleanup_interval_seconds") input.min = "60";
      label.appendChild(input);
      if (item.description) {
        const description = document.createElement("small");
        description.className = "config-description";
        description.textContent = item.description;
        label.appendChild(description);
      }
      fields.appendChild(label);
    });
    form.appendChild(group);
  });
}

async function loadConfig() {
  try {
    const data = await api("/api/v1/config?scope=system");
    renderConfig(data.items || []);
    $("configStatus").textContent = "配置已读取。敏感字段不会回显。";
  } catch (error) {
    $("configStatus").textContent = `读取配置失败：${error.message}`;
  }
}

function splitSkillValues(value) {
  return Array.isArray(value) ? value.join(", ") : "";
}

function parseSkillValues(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function normalizeToolNames(value) {
  return [...new Set((Array.isArray(value) ? value : []).map((item) => String(item || "").trim()).filter(Boolean))];
}

function toolAvailability(item) {
  if (!item) return { bindable: false, reason: "Tool 已不在 Registry 中" };
  if (item.toolType === "system_default") {
    return item.enabled && item.published
      ? { bindable: true, reason: "系统默认能力" }
      : { bindable: false, reason: "运行配置未启用" };
  }
  if (!item.enabled && !item.published) return { bindable: false, reason: "未启用、未发布" };
  if (!item.enabled) return { bindable: false, reason: "已停用" };
  if (!item.published) return { bindable: false, reason: "未发布" };
  return { bindable: true, reason: "已发布" };
}

function toolEffectLabel(item) {
  const label = { read: "只读", write: "写入", download: "下载" }[item?.sideEffect] || item?.sideEffect || "未分类";
  return item?.confirmationRequired ? `${label} · 需确认` : label;
}

function renderSkillToolBinding() {
  const chips = $("skillToolChips");
  const picker = $("skillToolPicker");
  const notice = $("skillToolBindingNotice");
  const count = $("skillToolSelectionCount");
  if (!chips || !picker || !notice || !count) return;

  const selected = normalizeToolNames(state.selectedSkillTools);
  state.selectedSkillTools = selected;
  const toolByName = new Map(state.tools.map((item) => [item.toolName, item]));
  chips.replaceChildren();
  if (!selected.length) {
    const empty = document.createElement("span");
    empty.className = "skill-tool-empty";
    empty.textContent = "尚未绑定 Tool";
    chips.appendChild(empty);
  } else {
    selected.forEach((toolName) => {
      const item = toolByName.get(toolName);
      const chip = document.createElement("span");
      chip.className = "skill-tool-chip";
      const label = document.createElement("span");
      label.textContent = item?.displayName ? `${toolName} · ${item.displayName}` : toolName;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "skill-tool-chip-remove";
      remove.dataset.action = "remove-skill-tool";
      remove.dataset.toolName = toolName;
      remove.title = `解除绑定 ${toolName}`;
      remove.setAttribute("aria-label", `解除绑定 ${toolName}`);
      remove.textContent = "×";
      chip.append(label, remove);
      chips.appendChild(chip);
    });
  }
  count.textContent = `已选 ${selected.length} 个`;

  const selectedItems = selected.map((toolName) => toolByName.get(toolName)).filter(Boolean);
  const sensitiveTools = selectedItems.filter((item) => item.sideEffect !== "read" || item.confirmationRequired);
  const unavailableTools = selected.filter((toolName) => !toolAvailability(toolByName.get(toolName)).bindable);
  if (unavailableTools.length) {
    notice.className = "skill-tool-binding-notice warning";
    notice.textContent = `有 ${unavailableTools.length} 个已绑定 Tool 当前不可用：${unavailableTools.join("、")}。发布 Skill 前必须解除绑定或恢复 Tool。`;
  } else if (sensitiveTools.length) {
    notice.className = "skill-tool-binding-notice caution";
    notice.textContent = `已绑定 ${sensitiveTools.length} 个具有副作用的 Tool；运行时将按 Tool 的确认策略执行。`;
  } else {
    notice.className = "skill-tool-binding-notice";
    notice.textContent = "当前绑定均为可用的只读 Tool。";
  }

  const query = $("skillToolSearchInput")?.value.trim().toLowerCase() || "";
  const unknownSelected = selected
    .filter((toolName) => !toolByName.has(toolName))
    .map((toolName) => ({ toolName, displayName: toolName, description: "此 Tool 已不在当前 Registry 中。", toolType: "unavailable", sideEffect: "unknown", enabled: false, published: false }));
  const groups = [
    ["系统默认能力", state.tools.filter((item) => item.toolType === "system_default")],
    ["业务 Tool", state.tools.filter((item) => item.toolType !== "system_default")],
    ["当前不可用的历史绑定", unknownSelected],
  ];
  picker.replaceChildren();
  let visible = 0;
  groups.forEach(([title, items]) => {
    const matches = items.filter((item) => {
      const haystack = [item.toolName, item.displayName, item.description, item.httpMethod, item.httpPath].join(" ").toLowerCase();
      return !query || haystack.includes(query);
    });
    if (!matches.length) return;
    visible += matches.length;
    const group = document.createElement("section");
    group.className = "skill-tool-group";
    const heading = document.createElement("h3");
    heading.textContent = title;
    group.appendChild(heading);
    matches.sort((left, right) => String(left.toolName).localeCompare(String(right.toolName))).forEach((item) => {
      const availability = toolAvailability(item);
      const selectedItem = selected.includes(item.toolName);
      const option = document.createElement("label");
      option.className = `skill-tool-option${selectedItem ? " selected" : ""}${availability.bindable ? "" : " unavailable"}`;
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = selectedItem;
      input.disabled = !availability.bindable;
      input.dataset.toolName = item.toolName;
      input.setAttribute("aria-label", `绑定 ${item.toolName}`);
      const content = document.createElement("span");
      content.className = "skill-tool-option-content";
      const name = document.createElement("strong");
      name.textContent = item.toolName;
      const detail = document.createElement("span");
      detail.className = "skill-tool-option-detail";
      detail.textContent = item.displayName || item.description || "未填写描述";
      const meta = document.createElement("span");
      meta.className = "skill-tool-option-meta";
      const endpoint = item.httpMethod && item.httpPath ? `${item.httpMethod} ${item.httpPath}` : "";
      meta.textContent = [item.toolType === "system_default" ? "系统默认" : item.toolType === "unavailable" ? "历史引用" : "业务 Tool", endpoint, toolEffectLabel(item), availability.reason].filter(Boolean).join(" · ");
      content.append(name, detail, meta);
      option.append(input, content);
      group.appendChild(option);
    });
    picker.appendChild(group);
  });
  if (!visible) {
    const empty = document.createElement("p");
    empty.className = "skill-tool-picker-empty";
    empty.textContent = query ? "没有匹配的 Tool。" : "尚无可显示的 Tool。";
    picker.appendChild(empty);
  }
}

function updateSelectedSkillTool(toolName, selected) {
  state.selectedSkillTools = selected
    ? normalizeToolNames([...state.selectedSkillTools, toolName])
    : state.selectedSkillTools.filter((item) => item !== toolName);
  renderSkillToolBinding();
}

function renderSkills(items) {
  state.skills = items;
  state.skillsLoaded = true;
  const body = $("skillsTable").querySelector("tbody");
  body.replaceChildren();
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="7" class="empty">暂未找到 system 作用域 Skill。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    row.dataset.skillId = item.skillId;
    const idCell = document.createElement("td");
    const id = document.createElement("code");
    id.className = "skill-id";
    id.textContent = item.skillId;
    idCell.appendChild(id);
    const nameCell = document.createElement("td");
    nameCell.className = "skill-name";
    nameCell.textContent = item.name || "—";
    const versionCell = document.createElement("td");
    versionCell.textContent = `v${item.version || 1}`;
    const sourceCell = document.createElement("td");
    sourceCell.textContent = `${item.source || "ops"} / ${item.scope || "system"}`;
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    status.className = `skill-badge skill-status-${String(item.status || "DRAFT").toLowerCase()}`;
    status.textContent = item.status || "DRAFT";
    statusCell.appendChild(status);
    const enabledCell = document.createElement("td");
    enabledCell.textContent = item.enabled ? "启用" : "停用";
    enabledCell.className = item.enabled ? "skill-enabled" : "skill-disabled";
    const actionCell = document.createElement("td");
    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "secondary skill-edit-button";
    editButton.dataset.action = "edit-skill";
    editButton.dataset.skillId = item.skillId;
    editButton.textContent = "编辑";
    actionCell.appendChild(editButton);
    row.append(idCell, nameCell, versionCell, sourceCell, statusCell, enabledCell, actionCell);
    body.appendChild(row);
  });
}

async function loadSkills(page = state.skillPage) {
  state.skillPage = page;
  try {
    const params = new URLSearchParams({ scope: "system", page: String(page), pageSize: String(state.skillPageSize) });
    const search = $("skillsSearchInput")?.value.trim();
    if (search) params.set("search", search);
    const data = await api(`/api/v1/skills?${params}`);
    state.skillPage = data.page || page;
    state.skillTotal = data.total || 0;
    renderSkills(data.items || []);
    renderPager("skillsPager", { page: state.skillPage, pageSize: data.pageSize || state.skillPageSize, total: state.skillTotal, onPage: loadSkills });
    $("skillsStatus").textContent = `已读取第 ${state.skillPage} 页 ${state.skills.length} / ${state.skillTotal} 个 system Skill；只有 PUBLISHED 且启用的版本会注入对应路由。`;
  } catch (error) {
    $("skillsStatus").textContent = `读取 Skills 失败：${error.message}`;
  }
}

function renderTools(items) {
  state.tools = items;
  state.toolsLoaded = true;
  if ($("skillDialog")?.open) renderSkillToolBinding();
  const body = $("toolsTable").querySelector("tbody");
  body.replaceChildren();
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">暂未注册业务 Tool；系统默认能力未启用。</td></tr>';
    return;
  }
  items.forEach((item) => {
    const row = document.createElement("tr");
    const typeCell = document.createElement("td");
    typeCell.textContent = item.toolType === "system_default" ? "系统默认" : "业务 Tool";
    typeCell.className = item.toolType === "system_default" ? "tool-system-default" : "tool-business";
    const name = document.createElement("td");
    const nameCode = document.createElement("code");
    nameCode.textContent = item.toolName || "";
    const nameLabel = document.createElement("small");
    nameLabel.textContent = item.displayName || "";
    name.append(nameCode, document.createElement("br"), nameLabel);
    const endpoint = document.createElement("td");
    endpoint.textContent = `${item.httpMethod} ${item.httpPath}`;
    const sideEffect = document.createElement("td");
    sideEffect.textContent = `${item.sideEffect || "read"}${item.confirmationRequired ? " · 需确认" : ""}`;
    const stateCell = document.createElement("td");
    stateCell.textContent = `${item.published ? "PUBLISHED" : "DRAFT"} · ${item.enabled ? "启用" : "停用"}`;
    const action = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.dataset.action = item.toolType === "system_default" ? "view-system-tool" : "edit-tool";
    button.dataset.toolName = item.toolName;
    button.textContent = item.toolType === "system_default" ? "查看配置" : "编辑";
    action.appendChild(button);
    row.append(typeCell, name, endpoint, sideEffect, stateCell, action);
    body.appendChild(row);
  });
}

async function loadTools(page = state.toolPage) {
  state.toolPage = page;
  try {
    const params = new URLSearchParams({ page: String(page), pageSize: String(state.toolPageSize) });
    const search = $("toolsSearchInput")?.value.trim();
    if (search) params.set("search", search);
    const data = await api(`/api/v1/tools?${params}`);
    state.toolPage = data.page || page;
    state.toolTotal = data.total || 0;
    renderTools(data.items || []);
    renderPager("toolsPager", { page: state.toolPage, pageSize: data.pageSize || state.toolPageSize, total: state.toolTotal, onPage: loadTools });
    $("toolsStatus").textContent = `已读取第 ${state.toolPage} 页 ${state.tools.length} / ${state.toolTotal} 个 Tool；接口方法和路径不能重复注册。`;
  } catch (error) {
    $("toolsStatus").textContent = `读取 Tools 失败：${error.message}`;
  }
}

function renderSwaggerOperations(items) {
  state.swaggerOperations = items;
  const body = $("swaggerOperationsTable").querySelector("tbody");
  body.replaceChildren();
  $("selectAllSwagger").checked = false;
  $("importSelectedToolsBtn").disabled = true;
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">Swagger 中没有可导入的接口。</td></tr>';
    return;
  }
  const registeredInterfaces = new Set(state.tools.map((tool) => tool.interfaceKey || `${tool.httpMethod} ${tool.httpPath}`.toLowerCase()));
  items.forEach((item) => {
    const row = document.createElement("tr");
    const selectCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.operationId = item.operationId || "";
    checkbox.dataset.interfaceKey = item.interfaceKey || "";
    checkbox.checked = false;
    checkbox.disabled = registeredInterfaces.has(item.interfaceKey);
    checkbox.title = checkbox.disabled ? "该 HTTP 接口已注册" : "选择此接口";
    selectCell.appendChild(checkbox);
    const operation = document.createElement("td");
    const operationCode = document.createElement("code");
    operationCode.textContent = item.operationId || "";
    operation.appendChild(operationCode);
    const method = document.createElement("td");
    method.textContent = item.httpMethod || "";
    const path = document.createElement("td");
    const pathCode = document.createElement("code");
    pathCode.textContent = item.httpPath || "";
    path.appendChild(pathCode);
    const description = document.createElement("td");
    description.textContent = item.displayName || "";
    row.append(selectCell, operation, method, path, description);
    body.appendChild(row);
  });
}

function updateSwaggerSelectionState() {
  const checkboxes = [...document.querySelectorAll("#swaggerOperationsTable input[data-operation-id]")];
  const available = checkboxes.filter((checkbox) => !checkbox.disabled);
  const selected = available.filter((checkbox) => checkbox.checked);
  $("importSelectedToolsBtn").disabled = selected.length === 0;
  const selectAll = $("selectAllSwagger");
  selectAll.checked = available.length > 0 && selected.length === available.length;
  selectAll.indeterminate = selected.length > 0 && selected.length < available.length;
}

function openSwaggerImporter() {
  $("swaggerStatus").textContent = "输入 Swagger URL 后读取接口。";
  renderSwaggerOperations([]);
  $("swaggerDialog").showModal();
}

function closeSwaggerImporter() {
  const dialog = $("swaggerDialog");
  if (dialog.open) dialog.close();
}

async function inspectSwagger() {
  const url = $("toolSwaggerUrl").value.trim();
  if (!url) return;
  $("inspectSwaggerBtn").disabled = true;
  $("swaggerStatus").textContent = "正在读取 Swagger…";
  try {
    const data = await api(`/api/v1/tools/swagger?swaggerUrl=${encodeURIComponent(url)}`);
    renderSwaggerOperations(data.items || []);
    const available = state.swaggerOperations.filter((item) => !state.tools.some((tool) => (tool.interfaceKey || `${tool.httpMethod} ${tool.httpPath}`.toLowerCase()) === item.interfaceKey));
    $("swaggerStatus").textContent = `Swagger 已读取 ${state.swaggerOperations.length} 个接口，其中 ${available.length} 个尚未注册。`;
  } catch (error) {
    $("swaggerStatus").textContent = `读取 Swagger 失败：${error.message}`;
  } finally {
    $("inspectSwaggerBtn").disabled = false;
  }
}

async function importSelectedSwaggerTools(event) {
  event.preventDefault();
  const swaggerUrl = $("toolSwaggerUrl").value.trim();
  const selectedIds = [...document.querySelectorAll("#swaggerOperationsTable input[data-operation-id]:checked")].map((checkbox) => checkbox.dataset.operationId).filter(Boolean);
  if (!swaggerUrl || !selectedIds.length) return;
  $("importSelectedToolsBtn").disabled = true;
  $("swaggerStatus").textContent = `正在导入 ${selectedIds.length} 个 Tool…`;
  let imported = 0;
  const failures = [];
  try {
    for (const operationId of selectedIds) {
      try {
        await api("/api/v1/tools/import", { method: "POST", body: JSON.stringify({ swaggerUrl, operationId }) });
        imported += 1;
      } catch (error) {
        failures.push(`${operationId}: ${error.message}`);
      }
    }
    await loadTools();
    closeSwaggerImporter();
    $("toolsStatus").textContent = failures.length
      ? `已导入 ${imported} 个 Tool；${failures.length} 个未导入：${failures.join("；")}`
      : `已导入 ${imported} 个 Tool；如需供模型使用，请编辑后启用并发布。`;
  } finally {
    $("importSelectedToolsBtn").disabled = false;
  }
}

function openToolEditor(toolName) {
  const item = state.tools.find((tool) => tool.toolName === toolName);
  if (!item) return;
  state.editingToolName = toolName;
  $("toolDialogTitle").textContent = `编辑 ${item.displayName || toolName}`;
  $("toolDialogMeta").textContent = `${item.toolName} · ${item.interfaceKey || `${item.httpMethod} ${item.httpPath}`}`;
  $("toolNameInput").value = item.toolName;
  $("toolDisplayNameInput").value = item.displayName || "";
  $("toolMethodInput").value = item.httpMethod || "";
  $("toolPathInput").value = item.httpPath || "";
  $("toolSideEffectInput").value = item.sideEffect || "read";
  $("toolConfirmationInput").checked = Boolean(item.confirmationRequired);
  $("toolEnabledInput").checked = Boolean(item.enabled);
  $("toolPublishedInput").checked = Boolean(item.published);
  $("toolDescriptionInput").value = item.description || "";
  $("toolParametersInput").value = JSON.stringify(item.parameters || {}, null, 2);
  $("toolResponseInput").value = JSON.stringify(item.responseSchema || {}, null, 2);
  $("toolMaskingPolicyInput").value = item.maskingPolicy || "default";
  $("toolDialog").showModal();
}

function closeToolEditor() {
  const dialog = $("toolDialog");
  if (dialog.open) dialog.close();
  state.editingToolName = null;
}

async function saveToolEditor(event) {
  event.preventDefault();
  const item = state.tools.find((tool) => tool.toolName === state.editingToolName);
  if (!item) return;
  let parameters;
  let responseSchema;
  try {
    parameters = JSON.parse($("toolParametersInput").value || "{}");
    responseSchema = JSON.parse($("toolResponseInput").value || "{}");
  } catch {
    $("toolsStatus").textContent = "参数或返回 Schema 必须是合法 JSON。";
    return;
  }
  try {
    await api(`/api/v1/tools/${encodeURIComponent(item.toolName)}`, { method: "PUT", body: JSON.stringify({
      displayName: $("toolDisplayNameInput").value.trim(), description: $("toolDescriptionInput").value,
      operationId: item.operationId, httpMethod: item.httpMethod, httpPath: item.httpPath,
      interfaceKey: item.interfaceKey, parameters, responseSchema, authStrategy: item.authStrategy,
      sideEffect: $("toolSideEffectInput").value, confirmationRequired: $("toolConfirmationInput").checked,
      rbacPolicy: item.rbacPolicy, maskingPolicy: $("toolMaskingPolicyInput").value.trim() || "default", swaggerSource: item.swaggerSource,
      source: item.source, version: item.version, enabled: $("toolEnabledInput").checked, published: $("toolPublishedInput").checked,
    }) });
    await loadTools();
    closeToolEditor();
    $("toolsStatus").textContent = "Tool 已保存。只有启用且发布的 Tool 才能被 Skill 绑定。";
  } catch (error) {
    $("toolsStatus").textContent = `保存 Tool 失败：${error.message}`;
  }
}

async function openSkillEditor(skillId) {
  const item = state.skills.find((skill) => skill.skillId === skillId);
  if (!item) return;
  state.skillDialogMode = "edit";
  state.editingSkillId = skillId;
  $("skillDialogTitle").textContent = `编辑 ${item.name || item.skillId}`;
  $("skillDialogMeta").textContent = `${item.skillId} · v${item.version || 1} · ${item.source || "ops"} / ${item.scope || "system"}`;
  $("skillIdInput").value = item.skillId || "";
  $("skillIdInput").disabled = true;
  $("skillNameInput").value = item.name || "";
  $("skillStatusInput").value = item.status || "DRAFT";
  $("skillEnabledInput").checked = Boolean(item.enabled);
  $("skillDomainInput").value = item.domain || "general";
  $("skillAliasesInput").value = splitSkillValues(item.aliases);
  state.selectedSkillTools = normalizeToolNames(item.allowedTools);
  $("skillToolSearchInput").value = "";
  $("skillDependenciesInput").value = splitSkillValues(item.dependencies);
  $("skillPositiveExamplesInput").value = splitSkillValues(item.positiveExamples);
  $("skillNegativeExamplesInput").value = splitSkillValues(item.negativeExamples);
  $("skillContentInput").value = item.content || "";
  $("saveSkillBtn").textContent = "保存 Skill";
  if (!state.toolsLoaded) await loadTools();
  renderSkillToolBinding();
  $("skillDialog").showModal();
  $("skillNameInput").focus();
}

async function openNewSkillEditor() {
  state.skillDialogMode = "create";
  state.editingSkillId = null;
  $("skillDialogTitle").textContent = "新增 Skill";
  $("skillDialogMeta").textContent = "创建后默认为 system 作用域、v1、DRAFT 且停用";
  $("skillIdInput").value = "";
  $("skillIdInput").disabled = false;
  $("skillNameInput").value = "";
  $("skillStatusInput").value = "DRAFT";
  $("skillEnabledInput").checked = false;
  $("skillDomainInput").value = "general";
  $("skillAliasesInput").value = "";
  state.selectedSkillTools = [];
  $("skillToolSearchInput").value = "";
  $("skillDependenciesInput").value = "";
  $("skillPositiveExamplesInput").value = "";
  $("skillNegativeExamplesInput").value = "";
  $("skillContentInput").value = "";
  $("saveSkillBtn").textContent = "创建 Skill";
  if (!state.toolsLoaded) await loadTools();
  renderSkillToolBinding();
  $("skillDialog").showModal();
  $("skillIdInput").focus();
}

function closeSkillEditor() {
  const dialog = $("skillDialog");
  if (dialog.open) dialog.close();
  state.editingSkillId = null;
  state.skillDialogMode = "edit";
  state.selectedSkillTools = [];
  $("skillIdInput").disabled = false;
}

async function saveSkillEditor(event) {
  event.preventDefault();
  const isCreating = state.skillDialogMode === "create";
  const skillId = $("skillIdInput").value.trim();
  const item = state.skills.find((skill) => skill.skillId === skillId);
  if (!isCreating && (!state.editingSkillId || !item)) {
    closeSkillEditor();
    return;
  }
  if (!skillId) {
    $("skillIdInput").focus();
    $("skillsStatus").textContent = "Skill ID 不能为空。";
    return;
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(skillId)) {
    $("skillIdInput").focus();
    $("skillsStatus").textContent = "Skill ID 只能包含字母、数字、点、下划线和短横线，且必须以字母或数字开头。";
    return;
  }
  if (isCreating && state.skills.some((skill) => skill.skillId === skillId)) {
    $("skillIdInput").focus();
    $("skillsStatus").textContent = `${skillId} 已存在，请使用其他 Skill ID。`;
    return;
  }
  const name = $("skillNameInput").value.trim();
  if (!name) {
    $("skillNameInput").focus();
    $("skillsStatus").textContent = `${skillId} 的名称不能为空。`;
    return;
  }
  $("saveSkillBtn").disabled = true;
  $("skillsStatus").textContent = `${isCreating ? "正在创建" : "正在保存"} ${skillId}…`;
  try {
    const payload = {
      name,
      version: Number(item?.version || 1),
      source: item?.source || "ops",
      status: $("skillStatusInput").value,
      scope: item?.scope || "system",
      enabled: $("skillEnabledInput").checked,
      allowedTools: normalizeToolNames(state.selectedSkillTools),
      dependencies: parseSkillValues($("skillDependenciesInput").value),
      domain: $("skillDomainInput").value.trim() || "general",
      aliases: parseSkillValues($("skillAliasesInput").value),
      positiveExamples: parseSkillValues($("skillPositiveExamplesInput").value),
      negativeExamples: parseSkillValues($("skillNegativeExamplesInput").value),
      content: $("skillContentInput").value,
    };
    if (isCreating) {
      payload.skillId = skillId;
      await api("/api/v1/skills", { method: "POST", body: JSON.stringify(payload) });
    } else {
      await api(`/api/v1/skills/${encodeURIComponent(state.editingSkillId)}`, { method: "PUT", body: JSON.stringify(payload) });
    }
    await loadSkills();
    closeSkillEditor();
    $("skillsStatus").textContent = isCreating ? "Skill 已创建；后续请求会使用已发布且启用的 Skill 内容。" : "Skill 已保存；后续请求会使用已发布且启用的 Skill 内容。";
  } catch (error) {
    $("skillsStatus").textContent = `保存 Skill 失败：${error.message}`;
  } finally {
    $("saveSkillBtn").disabled = false;
  }
}

function auditTime(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function auditCategoryLabel(value) {
  return { conversation: "对话", dsh: "DSH / Tool", llm: "LLM", runtime: "运行时" }[value] || value || "运行时";
}

function auditStatusLabel(value) {
  return { READY: "就绪", BUSY: "执行中", DEAD: "异常", CANCELLED: "已取消" }[value] || value || "未知";
}

function auditRecordSummary(item) {
  const payload = item.payload || {};
  const content = String(payload.content || "").replace(/\s+/g, " ").trim();
  switch (item.recordType) {
    case "user.message": return content ? `用户：${content.slice(0, 180)}` : "用户提交消息";
    case "assistant.message": return content ? `助手：${content.slice(0, 180)}` : "助手返回消息";
    case "assistant.status": return payload.message || "处理中…";
    case "assistant.chunk": return content ? `流式片段：${content.slice(0, 180)}` : "流式回答片段";
    case "assistant.welcome": return "初始化欢迎语";
    case "skill.route": return `Skill：${payload.skillId || payload.skill_id || "未标识"} · 模式：${payload.mode || "answer"}`;
    case "tool.call": return `调用 Tool：${payload.toolName || "未标识"}`;
    case "tool.result": return `Tool 结果：${payload.toolName || "未标识"} · ${payload.ok === false ? "失败" : "成功"}`;
    case "llm.request": return `LLM 请求：${payload.model || "未标识"} · ${Array.isArray(payload.messages) ? `${payload.messages.length} 条消息` : ""}`;
    case "llm.response": return content ? `LLM 回答：${content.slice(0, 180)}` : "LLM 返回完成";
    case "llm.thought": return content ? `LLM 思考：${content.slice(0, 180)}` : "LLM 思考内容";
    case "llm.error": return `LLM 异常：${String(payload.error || "未知错误").slice(0, 180)}`;
    case "runtime.error": return `运行异常：${String(payload.error || "未知错误").slice(0, 180)}`;
    case "turn.started": return "开始执行本轮请求";
    case "turn.completed": return "本轮执行完成";
    case "turn.cancelled": return "本轮执行已取消";
    default: return content ? content.slice(0, 180) : item.recordType || "审计记录";
  }
}

function renderAuditConversationList() {
  const list = $("auditConversationList");
  list.replaceChildren();
  const items = state.auditConversations;
  $("auditConversationCount").textContent = `${state.auditConversationTotal} 个`;
  if (!items.length) {
    list.innerHTML = '<div class="empty">暂无可查看的对话。</div>';
    return;
  }
  items.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `audit-conversation-item${item.conversationId === state.auditConversationId ? " active" : ""}`;
    button.dataset.conversationId = item.conversationId;
    const title = document.createElement("strong");
    title.textContent = item.title || item.conversationId || "未命名会话";
    const id = document.createElement("code");
    id.textContent = item.conversationId || "-";
    const meta = document.createElement("span");
    meta.className = "audit-conversation-meta";
    const owner = state.auditScope === "admin" && (item.ownerUserId || item.ownerTenantId)
      ? `账号 ${item.ownerUserId || "-"} · 租户 ${item.ownerTenantId || "-"}`
      : "";
    meta.textContent = [owner, auditStatusLabel(item.status), `${item.lastSeq || 0} 个事件`, auditTime(item.lastActivityAt || item.createdAt)].filter(Boolean).join(" · ");
    button.append(title, id, meta);
    list.appendChild(button);
  });
}

function renderAuditOverview(conversation) {
  const overview = $("auditOverview");
  overview.replaceChildren();
  if (!conversation) {
    overview.innerHTML = '<div class="empty">选择一个会话查看审计。</div>';
    return;
  }
  const title = state.auditConversations.find((item) => item.conversationId === conversation.conversationId)?.title;
  const heading = document.createElement("div");
  heading.className = "audit-overview-head";
  const headingText = document.createElement("div");
  const h3 = document.createElement("h3");
  h3.textContent = title || conversation.conversationId || "对话详情";
  const id = document.createElement("code");
  id.textContent = conversation.conversationId || "-";
  headingText.append(h3, id);
  const status = document.createElement("span");
  status.className = `audit-status audit-status-${String(conversation.status || "").toLowerCase()}`;
  status.textContent = auditStatusLabel(conversation.status);
  heading.append(headingText, status);
  const grid = document.createElement("div");
  grid.className = "audit-overview-grid";
  const auditIdentity = conversation.auditIdentity || {};
  const fields = [
    ["创建时间", auditTime(conversation.createdAt)],
    ["最近活动", auditTime(conversation.lastActivityAt)],
    ["登录账号", auditIdentity.account || "未记录"],
    ["当前角色", auditIdentity.currentRole || "未记录"],
    ["运行时", conversation.runtimeId || "尚未分配"],
    ["DSH Session", conversation.dshSessionId || "-"],
    ["Skill Profile", conversation.skillProfile || "default"],
    ["事件序号", String(conversation.lastSeq ?? 0)],
  ];
  if (state.auditScope === "admin" && conversation.owner) {
    fields.splice(2, 0, ["所属账号", conversation.owner.userId || "-"]);
    fields.splice(3, 0, ["所属租户", conversation.owner.tenantId || "-"]);
  }
  fields.forEach(([label, value]) => {
    const field = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const content = document.createElement("code");
    content.textContent = value;
    field.append(caption, content);
    grid.appendChild(field);
  });
  overview.append(heading, grid);
  if (conversation.lastError) {
    const error = document.createElement("p");
    error.className = "audit-error";
    error.textContent = `最近错误：${conversation.lastError}`;
    overview.appendChild(error);
  }
}

function renderAuditRecords(items) {
  const list = $("auditRecordList");
  list.replaceChildren();
  if (!items.length) {
    list.innerHTML = '<div class="empty">该会话暂无匹配的审计记录。</div>';
    return;
  }
  items.forEach((item) => {
    const record = document.createElement("article");
    const isSkillRoute = item.recordType === "skill.route";
    record.className = `audit-record audit-record-${String(item.category || "runtime").toLowerCase()}${isSkillRoute ? " audit-record-skill-route" : ""}`;
    const head = document.createElement("div");
    head.className = "audit-record-head";
    const category = document.createElement("span");
    category.className = "audit-category";
    category.textContent = auditCategoryLabel(item.category);
    const type = document.createElement("code");
    type.textContent = item.recordType || "unknown";
    const time = document.createElement("time");
    time.textContent = auditTime(item.createdAt);
    head.append(category, type, time);
    const summary = document.createElement("p");
    summary.className = "audit-record-summary";
    summary.textContent = auditRecordSummary(item);
    const meta = document.createElement("p");
    meta.className = "audit-record-meta";
    meta.textContent = [item.requestId && `request ${item.requestId}`, item.runtimeId && `runtime ${item.runtimeId}`].filter(Boolean).join(" · ") || "无请求关联 ID";
    const details = document.createElement("details");
    const detailsSummary = document.createElement("summary");
    detailsSummary.textContent = "查看原始审计数据";
    const payload = document.createElement("pre");
    payload.textContent = JSON.stringify(item.payload || {}, null, 2);
    details.append(detailsSummary, payload);
    record.append(head, summary, meta, details);
    list.appendChild(record);
  });
}

function renderAuditRecordLoadState() {
  const node = $("auditRecordLoadState");
  if (!node) return;
  if (!state.auditConversationId || !state.auditRecordTotal) {
    node.textContent = "";
    return;
  }
  node.textContent = state.auditRecordLoading
    ? "正在加载更多记录…"
    : `已加载 ${state.auditItems.length} / ${state.auditRecordTotal} 条记录`;
}

async function loadAuditDetail(conversationId, { append = false } = {}) {
  if (!conversationId) {
    state.auditRecordRequestId += 1;
    state.auditRecordLoading = false;
    state.auditRecordHasMore = false;
    state.auditRecordTotal = 0;
    state.auditItems = [];
    renderAuditOverview(null);
    renderAuditRecords([]);
    $("auditRecordLoadState").textContent = "";
    $("auditStatus").textContent = "尚未选择会话。";
    return;
  }
  const isSameConversation = state.auditConversationId === conversationId;
  if (state.auditRecordLoading && (append || isSameConversation)) return;
  const page = append && isSameConversation ? state.auditRecordPage + 1 : 1;
  if (append && (!state.auditRecordHasMore || !isSameConversation)) return;
  state.auditConversationId = conversationId;
  state.auditRecordPage = page;
  state.auditRecordLoading = true;
  const requestId = ++state.auditRecordRequestId;
  if (!append) {
    state.auditItems = [];
    state.auditRecordTotal = 0;
    state.auditRecordHasMore = false;
    renderAuditRecords([]);
  }
  renderAuditConversationList();
  renderAuditRecordLoadState();
  $("auditStatus").textContent = append ? "正在加载更多审计记录…" : "正在读取审计…";
  const category = $("auditCategoryFilter").value;
  const params = new URLSearchParams({ page: String(page), pageSize: String(state.auditRecordPageSize) });
  if (category) params.set("category", category);
  const search = $("auditRecordSearchInput")?.value.trim();
  if (search) params.set("search", search);
  try {
    const data = await consoleApi(`/api/v1/console/audit/conversations/${encodeURIComponent(conversationId)}?${params}`);
    if (requestId !== state.auditRecordRequestId) return;
    state.auditScope = data.scope || state.auditScope || "owner";
    const scopeNode = $("auditScope");
    if (scopeNode) scopeNode.textContent = state.auditScope === "admin" ? "管理员范围：全部账号 / 租户" : "当前账号范围";
    renderAuditConversationList();
    const receivedItems = data.items || [];
    state.auditItems = append ? [...state.auditItems, ...receivedItems] : receivedItems;
    state.auditRecordPage = data.page || page;
    state.auditRecordTotal = data.total || 0;
    state.auditRecordHasMore = state.auditItems.length < state.auditRecordTotal;
    renderAuditOverview(data.conversation);
    renderAuditRecords(state.auditItems);
    const sourceNote = data.source === "session_event_history" ? "（该会话使用历史事件兼容展示）" : "";
    $("auditStatus").textContent = `已加载 ${state.auditItems.length} / ${state.auditRecordTotal} 条记录${sourceNote}。`;
  } catch (error) {
    if (requestId !== state.auditRecordRequestId) return;
    if (!append) {
      state.auditItems = [];
      renderAuditOverview(null);
      renderAuditRecords([]);
    }
    $("auditStatus").textContent = `读取审计失败：${error.message}`;
  } finally {
    if (requestId === state.auditRecordRequestId) {
      state.auditRecordLoading = false;
      renderAuditRecordLoadState();
    }
  }
}

async function loadAuditConversations(page = state.auditConversationPage) {
  const requestedPage = Number(page);
  page = Number.isInteger(requestedPage) && requestedPage >= 1
    ? requestedPage
    : state.auditConversationPage;
  state.auditConversationPage = page;
  $("auditStatus").textContent = "正在读取会话列表…";
  try {
    const params = new URLSearchParams({ page: String(page), pageSize: String(state.auditConversationPageSize) });
    const search = $("auditConversationSearchInput")?.value.trim();
    if (search) params.set("search", search);
    const data = await consoleApi(`/api/v1/console/audit/conversations?${params}`);
    state.auditConversations = data.conversations || data.items || [];
    state.auditConversationPage = data.page || page;
    state.auditConversationTotal = data.total || 0;
    state.auditScope = data.scope || "owner";
    state.auditLoaded = true;
    const scopeNode = $("auditScope");
    if (scopeNode) scopeNode.textContent = state.auditScope === "admin" ? "管理员范围：全部账号 / 租户" : "当前账号范围";
    renderAuditConversationList();
    renderPager("auditConversationPager", {
      page: state.auditConversationPage,
      pageSize: data.pageSize || state.auditConversationPageSize,
      total: state.auditConversationTotal,
      onPage: loadAuditConversations,
    });
    const current = state.auditConversations.find((item) => item.conversationId === state.auditConversationId)
      || state.auditConversations.find((item) => item.conversationId === state.conversationId)
      || state.auditConversations[0];
    await loadAuditDetail(current?.conversationId || "");
  } catch (error) {
    state.auditLoaded = false;
    $("auditConversationList").innerHTML = `<div class="empty">读取会话失败：${error.message}</div>`;
    $("auditConversationPager").replaceChildren();
    $("auditStatus").textContent = "无法读取会话列表。";
  }
}

async function saveConfig() {
  const patch = {};
  document.querySelectorAll("#configForm [data-key]").forEach((input) => {
    if (input.disabled) return;
    const value = input.value.trim();
    if (input.dataset.secret === "1" && !value) return;
    if (value || input.dataset.multiline === "1") patch[input.dataset.key] = input.type === "number" ? Number(value) : value;
  });
  if (!Object.keys(patch).length) {
    $("configStatus").textContent = "没有需要保存的变更。";
    return;
  }
  try {
    const data = await api("/api/v1/config", { method: "PATCH", body: JSON.stringify({ scope: "system", patch }) });
    renderConfig(data.items || []);
    $("configStatus").textContent = "配置已保存；可热更新项已立即作用于后续请求，DB/Redis URL 需重启容器。";
  } catch (error) {
    $("configStatus").textContent = `保存配置失败：${error.message}`;
  }
}

function inferMimeType(fileName) {
  const name = String(fileName || "").toLowerCase();
  if (name.endsWith(".pdf")) return "application/pdf";
  if (name.endsWith(".png")) return "image/png";
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".webp")) return "image/webp";
  return "application/octet-stream";
}

function readAttachment() {
  const uploaded = state.attachment;
  const selectedFile = $("attachmentPicker").files?.[0];
  if (!uploaded) {
    if (selectedFile) throw new Error("附件正在上传或上传失败，请等待上传完成后再发送。");
    return null;
  }
  const fileRef = uploaded.fileRef;
  const fileName = $("attachmentName").value.trim() || uploaded.fileName;
  const mimeType = $("attachmentMime").value.trim() || uploaded.mimeType || inferMimeType(fileName);
  if (!fileName || !mimeType) throw new Error("附件文件名和 MIME 类型不能为空。");
  return { fileRef, fileName, mimeType, fileType: Number($("attachmentType").value) };
}

function extractUploadReference(value, depth = 0) {
  if (depth > 6 || value === null || value === undefined) return "";
  if (typeof value === "string" || typeof value === "number") return String(value).trim();
  if (Array.isArray(value)) return value.map((item) => extractUploadReference(item, depth + 1)).find(Boolean) || "";
  if (typeof value !== "object") return "";
  for (const key of ["url", "fileUrl", "filePath", "fileRef", "key", "objectKey", "objectName", "path", "data"]) {
    const reference = extractUploadReference(value[key], depth + 1);
    if (reference) return reference;
  }
  return "";
}

async function postAttachment(file, token) {
  const formData = new FormData();
  // Reuse the UMC Customer Portal upload endpoint. The verified portal
  // contract uses the plural multipart field name `files`.
  formData.append("files", file, file.name);
  const response = await fetch("/api/Document/Upload", {
    method: "POST",
    headers: { Authorization: token.toLowerCase().startsWith("bearer ") ? token : `Bearer ${token}` },
    body: formData,
  });
  const rawBody = await response.text();
  let payload = rawBody;
  try { payload = rawBody ? JSON.parse(rawBody) : rawBody; } catch { /* UMC may return a plain object key. */ }
  return { response, payload, fileRef: response.ok ? extractUploadReference(payload) : "" };
}

async function uploadAttachment(file) {
  try {
    await loadUmcToken();
  } catch {
    $("attachmentStatus").textContent = "UMC 自动登录失败，附件未上传。";
    $("attachmentPicker").value = "";
    return;
  }
  const rawToken = state.umcToken;
  state.attachment = null;
  $("attachmentRef").value = "";
  $("attachmentName").value = file.name;
  $("attachmentMime").value = file.type || inferMimeType(file.name);
  $("attachmentType").value = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf") ? "0" : "1";
  $("attachmentStatus").textContent = `正在上传 ${file.name}…`;
  try {
    let result = await postAttachment(file, rawToken);
    if (result.response.status === 401) {
      state.umcToken = "";
      $("umcToken").value = "";
      await loadUmcToken(true);
      result = await postAttachment(file, state.umcToken);
    }
    const { response, payload, fileRef } = result;
    if (!response.ok) throw new Error(typeof payload === "string" ? payload || `HTTP ${response.status}` : JSON.stringify(payload));
    if (!fileRef) throw new Error("上传成功但 UMC 响应中没有文件对象引用。");
    state.attachment = { fileRef, fileName: file.name, mimeType: file.type || inferMimeType(file.name) };
    $("attachmentRef").value = fileRef;
    $("attachmentStatus").textContent = `上传成功：${file.name}；现在可以发送问题或仅提交附件。`;
  } catch (error) {
    state.attachment = null;
    $("attachmentRef").value = "";
    $("attachmentPicker").value = "";
    $("attachmentStatus").textContent = `附件上传失败：${error.message}`;
  }
}

function clearAttachment() {
  $("attachmentPicker").value = "";
  $("attachmentRef").value = "";
  $("attachmentName").value = "";
  $("attachmentMime").value = "";
  $("attachmentType").value = "0";
  state.attachment = null;
  $("attachmentStatus").textContent = "附件已清除。";
}

function onAttachmentPicked(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  uploadAttachment(file);
}

function renderTestTable() {
  const body = $("testCasesTable").querySelector("tbody");
  body.innerHTML = "";
  if (!state.testCases.length) {
    body.innerHTML = '<tr><td colspan="10" class="empty">点击“生成测试用例”</td></tr>';
    $("runTestsBtn").disabled = true;
    return;
  }
  const resultMap = new Map(state.testResults.map((item) => [item.caseId, item]));
  state.testCases.forEach((item) => {
    const result = resultMap.get(item.caseId);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input type="checkbox" class="case-check" data-case-id="${item.caseId}" checked /></td><td><code>${item.caseId}</code></td><td>${item.language === "ar" ? "العربية" : "English"}</td><td>${item.label}</td><td><code>${item.skillId}</code></td><td class="question-cell"></td><td>${item.knowledgeRequired ? `${item.evidenceCount || 0} 条 · top_k ${item.topK}` : "—"}</td><td class="source-cell"></td><td class="result-cell">${result ? (result.timedOut ? "超时" : result.toolOk || result.guardrailRequired ? "通过" : "已返回") : "待执行"}</td><td>${result ? `${result.score}/5` : "—"}</td>`;
    tr.querySelector(".question-cell").textContent = item.question;
    tr.querySelector(".source-cell").textContent = item.sourceTitle || "—";
    body.appendChild(tr);
  });
  $("runTestsBtn").disabled = false;
}

function renderTestSummary(data) {
  const summary = $("testSummary");
  summary.innerHTML = `<span><strong>${data.count || 0}</strong> 条用例</span><span>语言：${(data.languages || []).join(" / ")}</span><span>知识库：${data.folderId || "未确定目录"}</span><span>三路：BM25 · Graph · Vector</span><span>top_k：${data.topK || 32}</span>`;
}

async function generateTests() {
  const languages = [];
  if ($("langEn").checked) languages.push("en");
  if ($("langAr").checked) languages.push("ar");
  if (!languages.length) {
    $("testLog").textContent = "至少选择一种语言。";
    return;
  }
  $("generateTestsBtn").disabled = true;
  $("testLog").textContent = "正在从实时知识库取证并组织测试用例…";
  try {
    const data = await api("/api/v1/test-cases/generate", { method: "POST", body: JSON.stringify({ languages, folderId: $("testFolderId").value.trim() || null, limit: Number($("testLimit").value || 36) }) });
    state.testCases = data.items || [];
    state.testResults = [];
    renderTestSummary(data);
    renderTestTable();
    $("testLog").textContent = `已生成 ${state.testCases.length} 条。知识库证据已写入每一行，可点击“执行已勾选”跑 DSH 端到端测试。`;
  } catch (error) {
    $("testLog").textContent = `生成失败：${error.message}`;
  } finally {
    $("generateTestsBtn").disabled = false;
  }
}

async function runTests() {
  const selectedIds = new Set([...document.querySelectorAll(".case-check:checked")].map((node) => node.dataset.caseId));
  const cases = state.testCases.filter((item) => selectedIds.has(item.caseId));
  if (!cases.length) {
    $("testLog").textContent = "请至少勾选一条用例。";
    return;
  }
  $("runTestsBtn").disabled = true;
  $("generateTestsBtn").disabled = true;
  $("testLog").textContent = `正在执行 ${cases.length} 条，单题最长 ${$("testTimeout").value} 秒；外部 Tool 和模型调用会产生实际请求。`;
  try {
    const data = await api("/api/v1/test-cases/run", { method: "POST", body: JSON.stringify({ cases, timeoutSeconds: Number($("testTimeout").value || 90) }) });
    state.testResults = data.items || [];
    renderTestTable();
    $("testLog").textContent = JSON.stringify({ count: data.count, completed: data.completed, routeMatches: data.routeMatches, toolSuccesses: data.toolSuccesses, averageScore: data.averageScore }, null, 2);
  } catch (error) {
    $("testLog").textContent = `执行失败：${error.message}`;
  } finally {
    $("runTestsBtn").disabled = false;
    $("generateTestsBtn").disabled = false;
  }
}

async function createConversation() {
  const data = await api("/api/v1/conversations", { method: "POST", body: JSON.stringify({ workspace: "demo", skill_profile: "default", runtime_profile: "default" }) });
  state.conversationId = data.conversationId;
  state.seq = 0;
  state.assistantNode = null;
  state.assistantContent = "";
  state.statusNode = null;
  $("conversationId").value = state.conversationId;
  $("runtimeId").textContent = data.runtimeId || "等待首条消息";
  $("lastSeq").textContent = data.lastSeq;
  $("events").innerHTML = "";
  addEvent("system", `已创建 ${state.conversationId}`, data.dshSessionId);
  if (state.ws?.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "subscribe", conversationId: state.conversationId, afterSeq: 0 }));
  }
  return data;
}

async function connect() {
  if (state.ws?.readyState === WebSocket.OPEN) return state.ws;
  if (state.connectPromise) return state.connectPromise;
  state.connectPromise = (async () => {
    await loadUmcToken();
    const previous = state.ws;
    if (previous && previous.readyState <= 1) previous.close();
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${location.host}/api/v1/ws?userId=${encodeURIComponent($("userId").value)}&tenantId=${encodeURIComponent($("tenantId").value)}`);
    state.ws = ws;
    const generation = ++state.wsGeneration;
    const ready = new Promise((resolve, reject) => {
      let settled = false;
      const fail = (error) => {
        if (!settled) {
          settled = true;
          reject(error);
        }
      };
      ws.onopen = async () => {
        try {
          setConnection("已连接", true);
          const umcToken = state.umcToken || $("umcToken")?.value.trim() || "";
          if (umcToken) ws.send(JSON.stringify({ type: "auth", umctoken: umcToken }));
          if (!state.conversationId && !$("conversationId").value) {
            await createConversation();
          } else {
            state.conversationId = state.conversationId || $("conversationId").value;
            ws.send(JSON.stringify({ type: "subscribe", conversationId: state.conversationId, afterSeq: state.seq }));
          }
          if (!settled) {
            settled = true;
            resolve(ws);
          }
        } catch (error) {
          setConnection("连接错误", false);
          fail(error);
        }
      };
      ws.onclose = (event) => {
        if (state.ws === ws && state.wsGeneration === generation) {
          if (event.code === 4401) {
            state.consoleAuthenticated = false;
            showConsoleGate("控制台会话已过期，请重新输入密码。", true);
          }
          setConnection("已断开", false);
        }
        fail(new Error(event.code === 4401 ? "控制台会话已过期" : "WebSocket 已断开"));
      };
      ws.onerror = () => {
        if (state.ws === ws && state.wsGeneration === generation) setConnection("连接错误", false);
        fail(new Error("WebSocket 连接失败"));
      };
      ws.onmessage = (message) => {
        let packet;
        try {
          packet = JSON.parse(message.data);
        } catch {
          addEvent("system.error", "收到无法解析的 WebSocket 消息。", "ws");
          return;
        }
        if (packet.type === "accepted") {
          $("requestId").textContent = packet.requestId || "-";
          $("runtimeId").textContent = packet.runtimeId || $("runtimeId").textContent;
        }
        if (packet.type === "error") {
          const messages = {
            conversation_not_found: "会话不存在或当前 UMC 身份/租户无权访问。请新建会话后重试。",
            identity_mismatch: "UMC 身份与当前用户 ID 不一致，请重新获取登录会话。",
            umc_token_required: "UMC 会话尚未准备好，请稍后重试。",
          };
          addEvent("system.error", messages[packet.code] || packet.code || "WebSocket 请求失败。", packet.code || "ws");
          return;
        }
        if (packet.type !== "event") return;
        state.seq = Math.max(state.seq, packet.seq || 0);
        $("lastSeq").textContent = state.seq;
        const data = packet.data || {};
        if (packet.eventType === "assistant.welcome") {
          addEvent("assistant.welcome", data.content || "", `seq ${packet.seq}`);
        } else if (packet.eventType === "assistant.status") {
          showAssistantStatus(data, `seq ${packet.seq}`);
        } else if (packet.eventType === "assistant.chunk") {
          clearAssistantStatus();
          if (!state.assistantNode) state.assistantNode = addEvent("assistant.message", "", `seq ${packet.seq}`);
          state.assistantContent += data.content || "";
          renderLocalizedContent(state.assistantNode, state.assistantContent);
          $("events").scrollTop = $("events").scrollHeight;
        } else if (packet.eventType === "assistant.message") {
          clearAssistantStatus();
          state.assistantContent = data.content || state.assistantContent;
          if (!state.assistantNode) state.assistantNode = addEvent("assistant.message", state.assistantContent, `seq ${packet.seq}`);
          else renderLocalizedContent(state.assistantNode, state.assistantContent);
        } else if (packet.eventType === "user.message") {
          clearAssistantStatus();
          const attachmentNote = data.attachment ? `附件：${data.attachment.fileName || "未命名文件"}` : "";
          addEvent("user.message", data.content || attachmentNote, `seq ${packet.seq}`);
          state.assistantNode = null;
          state.assistantContent = "";
        } else if (packet.eventType === "turn.completed") {
          clearAssistantStatus();
          addEvent(packet.eventType, JSON.stringify(data), `seq ${packet.seq}`);
          if (document.querySelector("#auditPanel.active") && state.auditConversationId === state.conversationId) {
            void loadAuditDetail(state.conversationId);
          }
        } else {
          if (packet.eventType === "runtime.error" || packet.eventType === "turn.cancelled") clearAssistantStatus();
          addEvent(packet.eventType, JSON.stringify(data), `seq ${packet.seq}`);
        }
      };
    });
    return await ready;
  })().finally(() => {
    state.connectPromise = null;
  });
  return state.connectPromise;
}

$("createBtn").addEventListener("click", async () => { try { await createConversation(); } catch (error) { addEvent("system.error", error.message); } });
$("connectBtn").addEventListener("click", () => { void connect().catch((error) => addEvent("system.error", error.message)); });
$("consoleAuthForm").addEventListener("submit", loginConsole);
$("consoleLogoutBtn").addEventListener("click", () => { void logoutConsole(); });
document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
$("reloadConfigBtn").addEventListener("click", loadConfig);
$("saveConfigBtn").addEventListener("click", saveConfig);
$("newSkillBtn").addEventListener("click", () => { void openNewSkillEditor(); });
$("reloadSkillsBtn").addEventListener("click", () => { void loadSkills(); });
$("skillsSearchInput").addEventListener("input", debounce(() => { void loadSkills(1); }));
$("newToolBtn").addEventListener("click", openSwaggerImporter);
$("reloadToolsBtn").addEventListener("click", () => { void loadTools(); });
$("toolsSearchInput").addEventListener("input", debounce(() => { void loadTools(1); }));
$("inspectSwaggerBtn").addEventListener("click", inspectSwagger);
$("swaggerImportForm").addEventListener("submit", importSelectedSwaggerTools);
$("cancelSwaggerBtn").addEventListener("click", closeSwaggerImporter);
$("closeSwaggerDialogBtn").addEventListener("click", closeSwaggerImporter);
$("selectAllSwagger").addEventListener("change", (event) => {
  document.querySelectorAll("#swaggerOperationsTable input[data-operation-id]:not(:disabled)").forEach((checkbox) => { checkbox.checked = event.target.checked; });
  updateSwaggerSelectionState();
});
$("swaggerOperationsTable").addEventListener("change", (event) => {
  if (event.target.matches("input[data-operation-id]")) updateSwaggerSelectionState();
});
$("reloadAuditBtn").addEventListener("click", () => { void loadAuditConversations(); });
$("auditConversationSearchInput").addEventListener("input", debounce(() => { void loadAuditConversations(1); }));
$("auditRecordSearchInput").addEventListener("input", debounce(() => { void loadAuditDetail(state.auditConversationId); }));
$("auditCategoryFilter").addEventListener("change", () => loadAuditDetail(state.auditConversationId));
$("auditConversationList").addEventListener("click", (event) => {
  const button = event.target.closest("[data-conversation-id]");
  if (button) void loadAuditDetail(button.dataset.conversationId);
});
$("auditRecordList").addEventListener("scroll", (event) => {
  const list = event.currentTarget;
  const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 160;
  if (nearBottom && state.auditRecordHasMore && !state.auditRecordLoading) {
    void loadAuditDetail(state.auditConversationId, { append: true });
  }
});
$("skillsTable").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action=edit-skill]");
  if (button) void openSkillEditor(button.dataset.skillId);
});
$("toolsTable").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action=edit-tool]");
  if (button) openToolEditor(button.dataset.toolName);
  const viewButton = event.target.closest("[data-action=view-system-tool]");
  if (viewButton) setTab("configPanel");
});
$("toolEditForm").addEventListener("submit", saveToolEditor);
$("cancelToolBtn").addEventListener("click", closeToolEditor);
$("closeToolDialogBtn").addEventListener("click", closeToolEditor);
$("toolDialog").addEventListener("click", (event) => {
  if (event.target === $("toolDialog")) closeToolEditor();
});
$("skillEditForm").addEventListener("submit", saveSkillEditor);
$("cancelSkillBtn").addEventListener("click", closeSkillEditor);
$("closeSkillDialogBtn").addEventListener("click", closeSkillEditor);
$("skillDialog").addEventListener("click", (event) => {
  if (event.target === $("skillDialog")) closeSkillEditor();
});
$("skillToolSearchInput").addEventListener("input", renderSkillToolBinding);
$("skillToolPicker").addEventListener("change", (event) => {
  const input = event.target.closest("input[data-tool-name]");
  if (input) updateSelectedSkillTool(input.dataset.toolName, input.checked);
});
$("skillToolChips").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action=remove-skill-tool]");
  if (button) updateSelectedSkillTool(button.dataset.toolName, false);
});
$("attachmentPicker").addEventListener("change", onAttachmentPicked);
$("clearAttachmentBtn").addEventListener("click", clearAttachment);
$("generateTestsBtn").addEventListener("click", generateTests);
$("runTestsBtn").addEventListener("click", runTests);
$("selectAllTests").addEventListener("change", (event) => document.querySelectorAll(".case-check").forEach((node) => { node.checked = event.target.checked; }));
$("messageForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = $("message").value.trim();
  let attachment = null;
  try {
    attachment = readAttachment();
  } catch (error) {
    $("attachmentStatus").textContent = error.message;
    return;
  }
  if (!content && !attachment) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    try {
      await connect();
    } catch (error) {
      $("attachmentStatus").textContent = `连接失败：${error.message}`;
      return;
    }
  }
  state.conversationId = state.conversationId || $("conversationId").value;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    $("attachmentStatus").textContent = "WebSocket 尚未连接，请先点击“连接 WS”。";
    return;
  }
  state.ws.send(JSON.stringify({ type: "message", conversationId: state.conversationId, content, attachment, clientMessageId: createClientMessageId() }));
  $("message").value = "";
  if (attachment) $("attachmentStatus").textContent = `已发送附件：${attachment.fileName}`;
});

// Unlock the console before obtaining the configured account's UMC session;
// operators should never need to paste a token before uploading or connecting.
void bootstrapConsole();
