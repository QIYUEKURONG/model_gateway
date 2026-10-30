const $ = (id) => document.getElementById(id);

const state = {
  slots: [
    { name: "city", kind: "variable", required: true, default: "" },
    { name: "nights", kind: "variable", required: false, default: "2" },
    { name: "policy", kind: "external", required: true, default: "" },
  ],
  selected: null,
  values: { city: "Paris", nights: "3" },
  materialText: { policy: "入住后不可取消。这里的 {city} 是原文，不会被替换。" },
};

const STEPS = ["validate", "render", "call", "stream", "ledger"];

function setHint(id, text, kind) {
  const node = $(id);
  node.textContent = text || "";
  node.className = "hint" + (kind ? " " + kind : "");
}

function renderSlots() {
  $("slots").innerHTML = state.slots.map((slot, index) => `
    <div class="slot-row">
      <input data-index="${index}" data-field="name" value="${escapeAttr(slot.name)}" placeholder="name">
      <select data-index="${index}" data-field="kind">
        <option value="variable" ${slot.kind === "variable" ? "selected" : ""}>可替换</option>
        <option value="external" ${slot.kind === "external" ? "selected" : ""}>外部资料</option>
      </select>
      <button type="button" data-remove="${index}">删除</button>
    </div>
  `).join("");
}

function renderBindings() {
  const variables = state.slots.filter((slot) => slot.kind === "variable");
  const externals = state.slots.filter((slot) => slot.kind === "external");
  const variableHtml = variables.map((slot) => `
    <label>${slot.name}
      <input data-var="${escapeAttr(slot.name)}" value="${escapeAttr(state.values[slot.name] || slot.default || "")}">
    </label>
  `).join("");
  const materialHtml = externals.map((slot) => `
    <label>${slot.name} · 外部资料，不参与替换
      <textarea data-material="${escapeAttr(slot.name)}" rows="3">${escapeText(state.materialText[slot.name] || "")}</textarea>
    </label>
  `).join("");
  $("bindings").innerHTML = `
    <div class="bind-block"><h3>可替换变量</h3>${variableHtml || "<p class='hint'>这条提示词没有变量</p>"}</div>
    <div class="bind-block"><h3>外部资料</h3>${materialHtml || "<p class='hint'>这条提示词没有外部资料</p>"}</div>
  `;
}

function readSlotsFromDom() {
  const next = state.slots.map((slot) => ({ ...slot }));
  document.querySelectorAll("#slots [data-field]").forEach((node) => {
    next[Number(node.dataset.index)][node.dataset.field] = node.value.trim();
  });
  state.slots = next.filter((slot) => slot.name);
}

function readBindingsFromDom() {
  document.querySelectorAll("[data-var]").forEach((node) => {
    state.values[node.dataset.var] = node.value;
  });
  document.querySelectorAll("[data-material]").forEach((node) => {
    state.materialText[node.dataset.material] = node.value;
  });
}

function promptPayload() {
  readSlotsFromDom();
  return {
    key: $("prompt-key").value.trim(),
    role: $("prompt-role").value,
    body: $("prompt-body").value,
    changelog: $("changelog").value,
    status: "active",
    slots: state.slots.map((slot) => ({
      name: slot.name,
      kind: slot.kind,
      description: "",
      required: slot.required !== false,
      default: slot.kind === "variable" && slot.default !== "" ? slot.default : null,
    })),
  };
}

function runPayload() {
  readBindingsFromDom();
  const materials = state.slots
    .filter((slot) => slot.kind === "external")
    .map((slot) => ({
      slot: slot.name,
      title: slot.name,
      content: state.materialText[slot.name] || "",
    }))
    .filter((item) => item.content);
  const body = {
    provider: $("provider").value,
    model: $("model").value.trim(),
    user_message: $("user-message").value,
    response_format: $("format").value,
    json_schema: $("schema").value,
  };
  if (state.selected) {
    const variables = {};
    state.slots.filter((slot) => slot.kind === "variable").forEach((slot) => {
      variables[slot.name] = state.values[slot.name] ?? "";
    });
    body.prompt = {
      key: state.selected.prompt_key,
      version: state.selected.version,
      variables,
      materials,
    };
  }
  return body;
}

function resetSteps() {
  document.querySelectorAll(".steps li").forEach((node) => {
    node.className = "";
  });
}

function markStep(step, status) {
  const node = document.querySelector(`.steps li[data-step="${step}"]`);
  if (node) node.className = status;
  const order = STEPS.indexOf(step);
  if (status === "active" || status === "done") {
    STEPS.slice(0, order).forEach((name) => {
      const previous = document.querySelector(`.steps li[data-step="${name}"]`);
      if (previous && previous.className !== "failed") previous.className = "done";
    });
  }
}

function showRender(data) {
  const variables = Object.entries(data.variables_used || {})
    .map(([key, value]) => `${key} = ${value}`)
    .join("\n");
  const materials = (data.materials || [])
    .map((item) => `${item.title || item.name}\n${item.content}`)
    .join("\n\n");
  $("render-view").innerHTML = `
    <div class="card variable"><strong>可替换变量 · v${data.version || ""}</strong>${escapeText(data.text || "没有模板")}${variables ? "\n\n" + escapeText(variables) : ""}</div>
    <div class="card external"><strong>外部资料 · 原文附件</strong>${escapeText(materials || "没有外部资料")}</div>
  `;
}

async function refreshStatus() {
  const data = await getJSON("/api/status");
  $("status").innerHTML = [
    pill("DeepSeek", data.deepseek_configured),
    pill("Anthropic", data.anthropic_configured),
  ].join("");
}

function pill(name, configured) {
  const label = configured ? "已配置" : "未配置密钥";
  return `<button type="button" class="pill ${configured ? "ok" : "bad"}" data-open-config>${name} ${label}</button>`;
}

async function openConfig() {
  setHint("config-hint", "");
  const data = await getJSON("/api/settings");
  $("deepseek-key").value = "";
  $("anthropic-key").value = "";
  $("clear-deepseek").checked = false;
  $("clear-anthropic").checked = false;
  $("deepseek-key").disabled = false;
  $("anthropic-key").disabled = false;
  $("deepseek-base").value = data.deepseek_base_url || "";
  $("anthropic-base").value = data.anthropic_base_url || "";
  $("deepseek-key").placeholder = data.deepseek_configured ? "已配置，留空则不修改" : "填入 DeepSeek 密钥";
  $("anthropic-key").placeholder = data.anthropic_configured ? "已配置，留空则不修改" : "填入 Anthropic 密钥";
  $("config").showModal();
}

async function saveConfig(event) {
  event.preventDefault();
  $("save-config").disabled = true;
  setHint("config-hint", "保存中…");
  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deepseek_api_key: $("deepseek-key").value,
        anthropic_api_key: $("anthropic-key").value,
        deepseek_base_url: $("deepseek-base").value.trim(),
        anthropic_base_url: $("anthropic-base").value.trim(),
        clear_deepseek: $("clear-deepseek").checked,
        clear_anthropic: $("clear-anthropic").checked,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || "保存失败");
    $("deepseek-key").value = "";
    $("anthropic-key").value = "";
    await refreshStatus();
    $("config").close();
  } catch (error) {
    setHint("config-hint", String(error.message || error), "bad");
  } finally {
    $("save-config").disabled = false;
  }
}

async function refreshPrompts() {
  const data = await getJSON("/api/prompts");
  const list = $("versions");
  list.innerHTML = "";
  data.prompts.slice().reverse().forEach((item) => {
    const li = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${item.prompt_key} · v${item.version} · ${item.status}`;
    button.addEventListener("click", () => selectPrompt(item));
    li.appendChild(button);
    list.appendChild(li);
  });
  if (!data.prompts.length) {
    list.innerHTML = "<li class='hint'>还没有版本。先发布一条提示词。</li>";
  }
}

function selectPrompt(item) {
  state.selected = item;
  state.slots = item.slots.map((slot) => ({
    name: slot.name,
    kind: slot.kind,
    required: slot.required,
    default: slot.default == null ? "" : String(slot.default),
  }));
  $("prompt-key").value = item.prompt_key;
  $("prompt-role").value = item.role;
  $("prompt-body").value = item.body;
  $("changelog").value = item.changelog || "";
  renderSlots();
  renderBindings();
  setHint("publish-hint", `当前使用 ${item.prompt_key} v${item.version}`, "ok");
}

async function refreshUsage() {
  const data = await getJSON("/api/usage");
  const summary = data.summary;
  $("stats").innerHTML = [
    stat("调用", summary.requests),
    stat("尝试错误率", percent(summary.error_rate)),
    stat("请求错误率", percent(summary.request_error_rate)),
    stat("花费 USD", usd(summary.cost_usd)),
    stat("平均延迟", `${Number(summary.avg_latency_ms).toFixed(0)} ms`),
    stat("总 token", summary.total_tokens),
  ].join("");
  $("calls").innerHTML = data.calls.map((row) => `
    <article class="call">
      <b class="${row.status === "success" ? "ok" : "bad"}">${row.status}</b>
      ${row.provider} / ${row.model}
      <div class="meta">${row.total_tokens} tokens · $${usd(row.cost_usd)} · ${Number(row.latency_ms).toFixed(0)} ms${row.error_code ? " · " + row.error_code : ""}</div>
    </article>
  `).join("") || "<p class='hint'>还没有调用记录</p>";
}

function usd(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value ?? "");
  const text = number.toFixed(8);
  return text.replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, ".00");
}

function stat(label, value) {
  return `<div><dt>${label}</dt><dd>${value}</dd></div>`;
}

async function publish() {
  setHint("publish-hint", "发布中…");
  const response = await fetch("/api/prompts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(promptPayload()),
  });
  const data = await response.json();
  if (!response.ok) {
    setHint("publish-hint", data.message, "bad");
    return;
  }
  setHint("publish-hint", `已发布 ${data.prompt_key} v${data.version}`, "ok");
  await refreshPrompts();
  selectPrompt(data);
}

async function preview() {
  if (!state.selected) {
    setHint("run-hint", "先发布或选择一个提示词版本", "bad");
    return;
  }
  resetSteps();
  markStep("render", "active");
  const response = await fetch("/api/render", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: runPayload().prompt }),
  });
  const data = await response.json();
  if (!response.ok) {
    markStep("render", "failed");
    setHint("run-hint", data.message, "bad");
    return;
  }
  markStep("render", "done");
  showRender(data);
  setHint("run-hint", "只做了渲染，还没有请求模型。", "ok");
}

let countdownTimer = null;

function startCountdown() {
  const started = Date.now();
  const node = $("countdown");
  node.hidden = false;
  const tick = () => {
    const elapsed = Math.floor((Date.now() - started) / 1000);
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const seconds = String(elapsed % 60).padStart(2, "0");
    node.textContent = `已等待 ${minutes}:${seconds}`;
  };
  tick();
  countdownTimer = setInterval(tick, 250);
}

function stopCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownTimer = null;
  const node = $("countdown");
  if (!node.hidden && node.textContent.startsWith("已等待")) {
    node.textContent = node.textContent.replace("已等待", "用时");
  }
}

function syncDelivery() {
  const waiting = $("delivery").value === "complete";
  const step = document.querySelector('.steps li[data-step="stream"]');
  if (step) step.textContent = waiting ? "等待完整结果" : "流式返回";
  if (!waiting) $("countdown").hidden = true;
}

async function send() {
  if ($("delivery").value === "complete") {
    await sendComplete();
    return;
  }
  $("countdown").hidden = true;
  resetSteps();
  $("output").textContent = "";
  $("parsed").hidden = true;
  setHint("run-hint", "");
  $("send").disabled = true;
  markStep("validate", "active");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runPayload()),
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let split = buffer.indexOf("\n\n");
      while (split >= 0) {
        handleFrame(buffer.slice(0, split));
        buffer = buffer.slice(split + 2);
        split = buffer.indexOf("\n\n");
      }
    }
  } catch (error) {
    markStep("call", "failed");
    setHint("run-hint", String(error), "bad");
  } finally {
    $("send").disabled = false;
    await refreshUsage();
  }
}

function handleFrame(frame) {
  const event = (frame.match(/^event:\s*(.+)$/m) || [])[1] || "message";
  const dataLine = (frame.match(/^data:\s*(.*)$/m) || [])[1] || "{}";
  let data = {};
  try { data = JSON.parse(dataLine); } catch { data = { message: dataLine }; }
  if (event === "stage") {
    if (data.step === "render" && !data.skipped) showRender(data);
    markStep(data.step === "ledger" ? "ledger" : data.step, "done");
    if (data.step === "call") markStep("stream", "active");
    return;
  }
  if (event === "message_start") {
    markStep("call", "done");
    markStep("stream", "active");
    return;
  }
  if (event === "content_delta") {
    $("output").textContent += data.text || "";
    return;
  }
  if (event === "usage") {
    setHint("run-hint", `${data.total_tokens} tokens · $${usd(data.cost_usd)} · ${data.latency_ms} ms · ${data.price_tier}`, "ok");
    return;
  }
  if (event === "message_stop") {
    markStep("stream", "done");
    if (data.parsed != null) {
      $("parsed").hidden = false;
      $("parsed").textContent = JSON.stringify(data.parsed, null, 2);
    }
    return;
  }
  if (event === "error") {
    markStep("stream", "failed");
    markStep("call", "failed");
    setHint("run-hint", `[${data.code}] ${data.message}`, "bad");
    if (!$("output").textContent) $("output").textContent = "这次调用没有模型正文。";
  }
}

async function sendComplete() {
  resetSteps();
  $("output").textContent = "等待完整结果，到齐后一次显示。";
  $("parsed").hidden = true;
  setHint("run-hint", "");
  $("send").disabled = true;
  markStep("call", "active");
  startCountdown();
  try {
    const response = await fetch("/api/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runPayload()),
    });
    const data = await response.json();
    if (!response.ok) {
      markStep("validate", "failed");
      setHint("run-hint", data.message || "请求失败", "bad");
      $("output").textContent = "这次调用没有模型正文。";
      return;
    }
    markStep("validate", "done");
    markStep("render", "done");
    if (data.render) showRender(data.render);
    markStep("call", data.ok ? "done" : "failed");
    markStep("stream", data.ok ? "done" : "failed");
    markStep("ledger", "done");
    if (data.ok) {
      $("output").textContent = data.content || "";
      if (data.parsed != null) {
        $("parsed").hidden = false;
        $("parsed").textContent = JSON.stringify(data.parsed, null, 2);
      }
      setHint(
        "run-hint",
        `${data.total_tokens} tokens · $${usd(data.cost_usd)} · ${data.latency_ms} ms · ${data.price_tier}`,
        "ok",
      );
    } else {
      $("output").textContent = "这次调用没有模型正文。";
      setHint("run-hint", `[${data.code}] ${data.message}`, "bad");
    }
  } catch (error) {
    markStep("call", "failed");
    setHint("run-hint", String(error), "bad");
  } finally {
    stopCountdown();
    $("send").disabled = false;
    await refreshUsage();
  }
}

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function percent(value) {
  return `${(Number(value) * 100).toFixed(0)}%`;
}

function escapeAttr(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
}

function escapeText(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;");
}

$("format").addEventListener("change", () => {
  $("schema-wrap").hidden = $("format").value !== "json_schema";
});
$("delivery").addEventListener("change", syncDelivery);
$("add-slot").addEventListener("click", () => {
  readSlotsFromDom();
  state.slots.push({ name: "", kind: "variable", required: true, default: "" });
  renderSlots();
});
$("slots").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove]");
  if (!button) return;
  readSlotsFromDom();
  state.slots.splice(Number(button.dataset.remove), 1);
  renderSlots();
  renderBindings();
});
$("slots").addEventListener("change", () => {
  readSlotsFromDom();
  renderBindings();
});
$("fill-sample").addEventListener("click", () => {
  state.selected = null;
  state.slots = [
    { name: "city", kind: "variable", required: true, default: "" },
    { name: "nights", kind: "variable", required: false, default: "2" },
    { name: "policy", kind: "external", required: true, default: "" },
  ];
  $("prompt-key").value = "trip.plan";
  $("prompt-body").value = "Plan {city} for {nights} nights.\n示例：{{\"city\": \"{city}\"}}";
  renderSlots();
  renderBindings();
});
$("status").addEventListener("click", (event) => {
  if (event.target.closest("[data-open-config]")) openConfig().catch((error) => setHint("run-hint", String(error), "bad"));
});
$("open-config").addEventListener("click", () => openConfig().catch((error) => setHint("run-hint", String(error), "bad")));
$("close-config").addEventListener("click", () => $("config").close());
$("config-form").addEventListener("submit", saveConfig);
$("clear-deepseek").addEventListener("change", () => {
  $("deepseek-key").disabled = $("clear-deepseek").checked;
});
$("clear-anthropic").addEventListener("change", () => {
  $("anthropic-key").disabled = $("clear-anthropic").checked;
});
$("publish").addEventListener("click", publish);
$("preview").addEventListener("click", preview);
$("send").addEventListener("click", send);
$("refresh").addEventListener("click", () => refreshUsage().catch((error) => setHint("run-hint", String(error), "bad")));

renderSlots();
renderBindings();
refreshStatus().catch((error) => setHint("run-hint", String(error), "bad"));
refreshPrompts().catch((error) => setHint("publish-hint", String(error), "bad"));
refreshUsage().catch((error) => setHint("run-hint", String(error), "bad"));
