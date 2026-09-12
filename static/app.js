/* ✍️ 写作共创坊 —— 前端逻辑 */
"use strict";

// ───────────────────────── 基础工具 ─────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  config: null,
  sessions: [],
  session: null,          // 当前会话完整数据
  sending: false,
  draftBusy: false,
  collectedSparks: new Set(),  // 已收下过的 💡 行（按文本去重，仅界面态）
};

function escapeHtml(s) {
  return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => (t.hidden = true), 2600);
}

function fmtTime(ts) {
  const d = new Date((ts || 0) * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hh}:${mm}`;
}

// 带鉴权的 fetch；401 时索要令牌重试一次
async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const token = localStorage.getItem("ws_token");
  if (token) headers["X-Access-Token"] = token;
  let r = await fetch(path, { ...options, headers });
  if (r.status === 401 && !headers["X-Access-Token"]) {
    const t = prompt("需要访问令牌（服务启动时控制台有打印）：");
    if (t) {
      localStorage.setItem("ws_token", t);
      headers["X-Access-Token"] = t;
      r = await fetch(path, { ...options, headers });
    }
  }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r;
}

async function apiJson(path, options = {}) {
  return (await api(path, options)).json();
}

// SSE 流读取：onEvent(dataObj)
async function sseFetch(path, body, onEvent) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("ws_token");
  if (token) headers["X-Access-Token"] = token;
  const r = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (r.status === 401) {
    const t = prompt("需要访问令牌：");
    if (t) {
      localStorage.setItem("ws_token", t);
      return sseFetch(path, body, onEvent);
    }
    throw new Error("需要访问令牌");
  }
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) {
          try { onEvent(JSON.parse(line.slice(6))); } catch (e) {}
        }
      }
    }
  }
}

// ───────────────────────── 会话 ─────────────────────────
async function loadSessions(pickFirst = false) {
  state.sessions = await apiJson("/api/sessions");
  renderCategoryFilter();
  renderSessionSelect();
  if (pickFirst && state.sessions.length && !state.session) {
    await openSession(state.sessions[0].id);
  }
}

function renderSessionSelect() {
  const filter = $("#categoryFilter").value || "all";
  const list = filter === "all" ? state.sessions
    : state.sessions.filter(s => s.category === filter);
  const sel = $("#sessionSelect");
  sel.innerHTML = "";
  for (const s of list) {
    const opt = document.createElement("option");
    opt.value = s.id;
    opt.textContent = s.title + (s.spark_count ? ` 💡${s.spark_count}` : "");
    if (state.session && s.id === state.session.id) opt.selected = true;
    sel.appendChild(opt);
  }
  if (!list.length) {
    const opt = document.createElement("option");
    opt.textContent = filter === "all" ? "（还没有话题）" : "（该分类下暂无话题）";
    sel.appendChild(opt);
  }
}

function renderCategoryFilter() {
  const cats = (state.config && state.config.topic_categories) || {};
  const counts = {};
  for (const s of state.sessions) {
    if (s.category) counts[s.category] = (counts[s.category] || 0) + 1;
  }
  const sel = $("#categoryFilter");
  const prev = sel.value || "all";
  sel.innerHTML = "";
  const all = document.createElement("option");
  all.value = "all";
  all.textContent = `全部分类（${state.sessions.length}）`;
  sel.appendChild(all);
  for (const [k, label] of Object.entries(cats)) {
    if (!counts[k]) continue;
    const o = document.createElement("option");
    o.value = k;
    o.textContent = `${label}（${counts[k]}）`;
    sel.appendChild(o);
  }
  sel.value = prev === "all" || counts[prev] ? prev : "all";
}

async function openSession(id) {
  state.session = await apiJson(`/api/sessions/${id}`);
  renderSession();
  renderSparks();
  renderDrafts();
  refreshFeishu();
}

function renderSession() {
  const s = state.session;
  if (!s) return;
  $("#chatEmpty").style.display = s.messages.length ? "none" : "";
  $("#topicTitle").textContent = s.title;
  $("#modeSelect").value = s.mode;
  renderAnalysis();

  const list = $("#chatList");
  list.innerHTML = "";
  for (const m of s.messages) list.appendChild(renderMessage(m));
  scrollChatBottom();
}

// 话题自动分析结果 → 分类徽标 + 关键词 + 一句话定位
function renderAnalysis() {
  const s = state.session;
  const box = $("#topicTags");
  if (!s || !s.topic_analysis || !s.topic_analysis.category) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  const ta = s.topic_analysis;
  const cats = (state.config && state.config.topic_categories) || {};
  const fmts = (state.config && state.config.draft_formats) || {};
  box.innerHTML = "";
  const catChip = document.createElement("span");
  catChip.className = "cat-chip";
  catChip.dataset.cat = ta.category;
  catChip.textContent = cats[ta.category] || ta.category;
  catChip.title = "话题分类（自动分析，点 🏷 分析可重跑）";
  box.appendChild(catChip);
  // 素材成熟度：进度条 + 提示（≥80 视为可以动笔）
  if (typeof ta.maturity === "number") {
    const m = document.createElement("span");
    const ready = ta.maturity >= 80;
    m.className = "maturity" + (ready ? " ready" : "");
    m.title = ta.maturity_hint || "";
    m.innerHTML = `<span class="maturity-bar"><span class="maturity-fill" style="width:${ta.maturity}%"></span></span>` +
      `<span class="maturity-text">${ready ? "✍️ 可以动笔了" : ta.maturity_hint || "素材 " + ta.maturity + "%"}</span>`;
    box.appendChild(m);
  }
  for (const t of ta.tags || []) {
    const chip = document.createElement("span");
    chip.className = "tag-chip";
    chip.textContent = t;
    box.appendChild(chip);
  }
  if (ta.summary) {
    const sum = document.createElement("span");
    sum.className = "topic-analysis-summary";
    sum.textContent = ta.summary;
    box.appendChild(sum);
  }
  if ((ta.recommended_formats || []).length) {
    const rec = document.createElement("span");
    rec.className = "topic-analysis-summary";
    rec.textContent = "→ 适合：" + ta.recommended_formats.map(f => fmts[f] || f).join("、");
    box.appendChild(rec);
  }
  box.hidden = false;
}

// ───────────────────────── 消息渲染 ─────────────────────────
// AI 消息里以 💡 开头的行 → 灵感卡片行
function renderMessage(m, streamEl = null) {
  const div = document.createElement("div");
  div.className = `msg ${m.role === "user" ? "user" : "ai"}`;

  const role = document.createElement("div");
  role.className = "msg-role";
  role.textContent = m.role === "user" ? "🙋 我" : "✍️ 搭档";
  div.appendChild(role);

  const body = document.createElement("div");
  body.className = "msg-body";

  if (m.role === "assistant" && streamEl === null) {
    // 完整渲染：拆出 💡 行
    const lines = (m.content || "").split("\n");
    const plain = [], sparkLines = [];
    for (const line of lines) {
      if (/^\s*💡/.test(line)) sparkLines.push(line.replace(/^\s*💡\s*/, "").trim());
      else plain.push(line);
    }
    body.textContent = plain.join("\n").trim() || "…";
    for (const sp of sparkLines) body.appendChild(buildSparkLine(sp));
  } else if (streamEl !== null) {
    body.appendChild(streamEl); // 流式中的临时元素
  } else {
    body.textContent = m.content;
  }
  div.appendChild(body);

  if (m.role === "user") {
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const btn = document.createElement("button");
    btn.className = "msg-action";
    btn.textContent = "⭐ 收为灵感";
    btn.onclick = () => openSparkModal(m.content.slice(0, 500), "user");
    actions.appendChild(btn);
    div.appendChild(actions);
  }
  return div;
}

function buildSparkLine(text) {
  const wrap = document.createElement("div");
  wrap.className = "spark-line";
  const span = document.createElement("span");
  span.className = "spark-text";
  span.textContent = "💡 " + text;
  const btn = document.createElement("button");
  btn.className = "spark-collect";
  btn.textContent = "收下";
  if (state.collectedSparks.has(text)) {
    btn.classList.add("done");
    btn.textContent = "已收下";
  }
  btn.onclick = async () => {
    try {
      await api("/api/sessions/" + state.session.id + "/sparks", {
        method: "POST",
        body: JSON.stringify({ text, origin: "ai" }),
      });
      state.collectedSparks.add(text);
      btn.classList.add("done");
      btn.textContent = "已收下";
      toast("已收进灵感卡片 💡");
      refreshSessionData();
    } catch (e) { toast(e.message, true); }
  };
  wrap.appendChild(span);
  wrap.appendChild(btn);
  return wrap;
}

function scrollChatBottom() {
  const sc = $("#chatScroll");
  sc.scrollTop = sc.scrollHeight;
}

// ───────────────────────── 聊天 ─────────────────────────
async function sendMessage(text) {
  if (state.sending || !text.trim()) return;
  if (!state.session) { toast("先新建一个话题", true); return; }
  if (!state.config.llm.has_key) { toast("先在 ⚙ 设置里配置大模型 API Key", true); openSettings(); return; }

  state.sending = true;
  setSending(true);
  $("#chatEmpty").style.display = "none";

  // 立即渲染用户消息 + AI 占位
  const list = $("#chatList");
  const userMsg = { role: "user", content: text, ts: Date.now() / 1000 };
  list.appendChild(renderMessage(userMsg));

  const typing = document.createElement("span");
  typing.className = "typing";
  typing.innerHTML = "<i></i><i></i><i></i>";
  const aiDiv = renderMessage({ role: "assistant", content: "" }, typing);
  list.appendChild(aiDiv);
  scrollChatBottom();

  let full = "";
  try {
    await sseFetch(`/api/sessions/${state.session.id}/chat`, { message: text }, (ev) => {
      if (ev.t === "delta") {
        full += ev.v;
        const body = aiDiv.querySelector(".msg-body");
        if (typing.parentNode) typing.remove();
        // 流式期间纯文本展示（💡 行等结束后再结构化渲染）
        body.textContent = full;
        scrollChatBottom();
      } else if (ev.t === "done") {
        if (ev.title) {
          state.session.title = ev.title;
          $("#topicTitle").textContent = ev.title;
          loadSessions();
        }
        // 话题分析在后台跑，延迟再刷一次拿结果
        setTimeout(refreshSessionData, 2600);
      } else if (ev.t === "error") {
        throw new Error(ev.v);
      }
    });
    // 用会话数据重渲染最后一条（结构化 💡 行）
    await refreshSessionData();
    setTimeout(refreshFeishu, 2500);
  } catch (e) {
    toast("生成失败：" + e.message, true);
    if (typing.parentNode) typing.remove();
    if (!full) aiDiv.remove();
    refreshSessionData();
  } finally {
    state.sending = false;
    setSending(false);
  }
}

function setSending(b) {
  $("#btnSend").disabled = b;
  $("#btnSend").textContent = b ? "…" : "发送";
  $("#inputBox").disabled = false;
}

async function refreshSessionData() {
  if (!state.session) return;
  try {
    state.session = await apiJson(`/api/sessions/${state.session.id}`);
    renderSession();
    renderSparks();
    renderDrafts();
    await loadSessions();
    renderWeekStats();
  } catch (e) { /* 会话可能被删 */ }
}

// 手动重跑话题分析
async function runAnalysis() {
  if (!state.session) return;
  if (!state.session.messages.length) { toast("还没有讨论内容", true); return; }
  $("#btnAnalyze").disabled = true;
  try {
    const r = await apiJson(`/api/sessions/${state.session.id}/analyze`, { method: "POST" });
    await refreshSessionData();
    toast("已重新分析 🏷");
  } catch (e) { toast(e.message, true); }
  finally { $("#btnAnalyze").disabled = false; }
}

// 文案工坊：搭档推荐格式 → 可点选
function renderRecRow() {
  const row = $("#draftRecRow");
  const ta = state.session && state.session.topic_analysis;
  const fmts = (state.config && state.config.draft_formats) || {};
  const recs = (ta && ta.recommended_formats) || [];
  row.innerHTML = `<span class="rec-label">🏷 搭档推荐：</span>`;
  if (!recs.length) {
    row.hidden = true;
    return;
  }
  for (const f of recs) {
    const chip = document.createElement("button");
    chip.className = "rec-chip";
    chip.textContent = fmts[f] || f;
    chip.onclick = () => { $("#draftFormat").value = f; };
    row.appendChild(chip);
  }
  row.hidden = false;
}

// ───────────────────────── 灵感 ─────────────────────────
const ORIGIN_LABEL = { ai: "来自搭档标记", user: "来自我的发言", manual: "手动记录" };

function renderSparks() {
  const s = state.session;
  const list = $("#sparkList");
  if (!s || !s.sparks.length) {
    list.innerHTML = `<div class="tab-empty">还没有灵感<br>聊起来之后，好句子会出现在这里</div>`;
    updateBadges();
    return;
  }
  list.innerHTML = "";
  for (const sp of [...s.sparks].reverse()) {
    const card = document.createElement("div");
    card.className = "spark-card";
    card.innerHTML = `
      <div class="spark-origin">${ORIGIN_LABEL[sp.origin] || sp.origin} · ${fmtTime(sp.ts)}</div>
      <div class="spark-content">${escapeHtml(sp.text)}</div>
      ${sp.note ? `<div class="spark-note">※ ${escapeHtml(sp.note)}</div>` : ""}
      <div class="card-actions">
        <button class="mini-btn" data-act="edit">编辑</button>
        <button class="mini-btn danger" data-act="del">删除</button>
      </div>`;
    card.querySelector('[data-act="edit"]').onclick = () => openSparkModal(sp.text, "manual", sp.note, sp.id);
    card.querySelector('[data-act="del"]').onclick = async () => {
      if (!confirm("删除这条灵感？")) return;
      try {
        await api(`/api/sessions/${s.id}/sparks/${sp.id}`, { method: "DELETE" });
        await refreshSessionData();
        toast("已删除");
      } catch (e) { toast(e.message, true); }
    };
    list.appendChild(card);
  }
  updateBadges();
}

function updateBadges() {
  const s = state.session;
  const sb = $("#sparkBadge"), db = $("#draftBadge");
  const sc = s ? s.sparks.length : 0, dc = s ? s.drafts.length : 0;
  sb.hidden = !sc; sb.textContent = sc;
  db.hidden = !dc; db.textContent = dc;
}

// 灵感弹窗（新增 / 编辑共用；spid 有值为编辑）
let sparkEditId = null;
function openSparkModal(text = "", origin = "manual", note = "", spid = null) {
  sparkEditId = spid;
  $("#sparkModalTitle").textContent = spid ? "编辑灵感" : "记一条灵感";
  $("#sparkText").value = text;
  $("#sparkNote").value = note;
  $("#sparkText").dataset.origin = origin;
  showModal("#modalSpark");
}

async function saveSparkModal() {
  const text = $("#sparkText").value.trim();
  if (!text) { toast("内容不能为空", true); return; }
  const sid = state.session.id;
  try {
    if (sparkEditId) {
      await api(`/api/sessions/${sid}/sparks/${sparkEditId}`, {
        method: "PATCH",
        body: JSON.stringify({ text, note: $("#sparkNote").value }),
      });
    } else {
      await api(`/api/sessions/${sid}/sparks`, {
        method: "POST",
        body: JSON.stringify({ text, note: $("#sparkNote").value, origin: $("#sparkText").dataset.origin }),
      });
    }
    hideModal("#modalSpark");
    await refreshSessionData();
    toast("已保存 💡");
  } catch (e) { toast(e.message, true); }
}

// ───────────────────────── 文案 ─────────────────────────
function fillDraftOptions() {
  const fill = (sel, dict) => {
    sel.innerHTML = "";
    for (const [k, v] of Object.entries(dict)) {
      const o = document.createElement("option");
      o.value = k; o.textContent = v;
      sel.appendChild(o);
    }
  };
  fill($("#draftFormat"), state.config.draft_formats);
  fill($("#draftTone"), state.config.draft_tones);
  fill($("#draftLength"), state.config.draft_lengths);
}

function renderDrafts() {
  const s = state.session;
  const list = $("#draftList");
  if (!s || !s.drafts.length) {
    list.innerHTML = `<div class="tab-empty">还没有文案<br>聊出素材后点「生成文案」</div>`;
    updateBadges();
    return;
  }
  list.innerHTML = "";
  for (const d of s.drafts) list.appendChild(buildDraftCard(d));
  updateBadges();
}

function buildDraftCard(d) {
  const s = state.session;
  const fmtLabel = (state.config.draft_formats || {})[d.format] || d.format;
  const card = document.createElement("div");
  card.className = "draft-card";

  const head = document.createElement("div");
  head.className = "draft-card-head";
  head.innerHTML = `
    <span class="draft-title">✍️ ${escapeHtml(fmtLabel)}</span>
    <span class="draft-time">${fmtTime(d.updated_at)} · ${d.content.length} 字${d.history.length ? ` · 改${d.history.length}稿` : ""}</span>`;
  head.onclick = () => body.classList.toggle("collapsed");
  card.appendChild(head);

  const body = document.createElement("div");
  body.className = "draft-card-body";

  const ta = document.createElement("textarea");
  ta.className = "draft-content";
  ta.value = d.content;
  body.appendChild(ta);

  const reviseRow = document.createElement("div");
  reviseRow.className = "draft-revise-row";
  const reviseInput = document.createElement("input");
  reviseInput.placeholder = "修改指令，例如：开头换个故事；压缩到一半长度…（Enter 执行）";
  reviseInput.onkeydown = (e) => {
    if (e.key === "Enter" && reviseInput.value.trim()) reviseBtn.click();
  };
  const reviseBtn = document.createElement("button");
  reviseBtn.className = "btn btn-ghost btn-sm";
  reviseBtn.textContent = "改写";
  reviseBtn.onclick = async () => {
    const ins = reviseInput.value.trim();
    if (!ins) { toast("先写修改指令", true); return; }
    await reviseDraft(d.id, ins, card);
  };
  reviseRow.appendChild(reviseInput);
  reviseRow.appendChild(reviseBtn);
  body.appendChild(reviseRow);

  const actions = document.createElement("div");
  actions.className = "draft-actions";
  const mkBtn = (label, fn, primary = false) => {
    const b = document.createElement("button");
    b.className = primary ? "btn btn-primary" : "btn btn-ghost";
    b.textContent = label;
    b.onclick = fn;
    actions.appendChild(b);
    return b;
  };
  mkBtn("💾 保存修改", async () => {
    try {
      await api(`/api/sessions/${s.id}/drafts/${d.id}`, {
        method: "PATCH", body: JSON.stringify({ content: ta.value }),
      });
      await refreshSessionData();
      toast("已保存");
    } catch (e) { toast(e.message, true); }
  });
  mkBtn("📋 复制", async () => {
    await navigator.clipboard.writeText(ta.value);
    toast("已复制到剪贴板");
  });
  mkBtn("⬇ 下载 .md", () => {
    const blob = new Blob([ta.value], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${s.title}-${fmtLabel}.md`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  mkBtn("💎 打磨", async () => {
    await polishDraft(d.id, card);
  });
  mkBtn("📘 存进话题文档", async (e) => {
    await sendDraftToFeishu(d.id, false);
  });
  mkBtn("📘 单独成文", async () => {
    await sendDraftToFeishu(d.id, true);
  });
  mkBtn("🗑", async () => {
    if (!confirm("删除这版文案？")) return;
    try {
      await api(`/api/sessions/${s.id}/drafts/${d.id}`, { method: "DELETE" });
      await refreshSessionData();
    } catch (err) { toast(err.message, true); }
  });
  body.appendChild(actions);
  card.appendChild(body);
  return card;
}

// 流式生成 / 改写共用的展示块
function buildStreamBox(label) {
  const box = document.createElement("div");
  box.className = "draft-stream";
  box.innerHTML = `<div class="stream-label">${escapeHtml(label)} 正在整理…</div><div class="stream-text"></div>`;
  return box;
}

async function generateDraft() {
  if (state.draftBusy) return;
  if (!state.session) { toast("先新建一个话题", true); return; }
  if (!state.session.messages.length) { toast("先聊出一些素材，再来生成文案", true); return; }
  state.draftBusy = true;
  $("#btnGenDraft").disabled = true;

  const box = buildStreamBox("✍️");
  $("#draftList").prepend(box);
  box.scrollIntoView({ behavior: "smooth" });

  try {
    await sseFetch(`/api/sessions/${state.session.id}/drafts`, {
      format: $("#draftFormat").value,
      tone: $("#draftTone").value,
      length: $("#draftLength").value,
      extra: $("#draftExtra").value.trim(),
      spark_ids: pickedSparkIds(),
    }, (ev) => {
      if (ev.t === "delta") {
        box.querySelector(".stream-text").textContent += ev.v;
        box.scrollTop = box.scrollHeight;
      } else if (ev.t === "error") {
        throw new Error(ev.v);
      }
    });
    $("#draftComposer").hidden = true;
    $("#draftExtra").value = "";
    await refreshSessionData();
    // 自动打磨：编辑二遍稿
    if ($("#autoPolish").checked && state.session.drafts.length) {
      const newest = state.session.drafts[0];
      await polishDraft(newest.id);
    } else {
      toast("文案已生成 ✍️");
    }
  } catch (e) {
    toast("生成失败：" + e.message, true);
  } finally {
    box.remove();
    state.draftBusy = false;
    $("#btnGenDraft").disabled = false;
  }
}

async function reviseDraft(did, instruction, cardEl) {
  if (state.draftBusy) return;
  state.draftBusy = true;

  const box = buildStreamBox("✍️ 按你的指令改写中");
  cardEl.querySelector(".draft-card-body").prepend(box);

  try {
    await sseFetch(`/api/sessions/${state.session.id}/drafts/${did}/revise`, {
      instruction,
    }, (ev) => {
      if (ev.t === "delta") {
        box.querySelector(".stream-text").textContent += ev.v;
        box.scrollTop = box.scrollHeight;
      } else if (ev.t === "error") {
        throw new Error(ev.v);
      }
    });
    await refreshSessionData();
    toast("改写完成 ✍️");
  } catch (e) {
    toast("改写失败：" + e.message, true);
  } finally {
    box.remove();
    state.draftBusy = false;
  }
}

async function sendDraftToFeishu(did, separate) {
  try {
    const r = await apiJson(`/api/sessions/${state.session.id}/drafts/${did}/feishu`, {
      method: "POST",
      body: JSON.stringify({ separate }),
    });
    toast(separate ? "已在飞书单独建文 ✅" : "已追加进话题文档 ✅");
    if (r.url) window.open(r.url, "_blank");
  } catch (e) {
    toast(e.message, true);
  }
}

// 打磨：编辑红笔二遍稿（流式替换内容）
async function polishDraft(did, cardEl = null) {
  if (state.draftBusy) return;
  state.draftBusy = true;
  const box = buildStreamBox("💎 编辑打磨中（去 AI 味 · 锻金句）");
  const body = cardEl ? cardEl.querySelector(".draft-card-body") : $("#draftList");
  body.prepend(box);
  try {
    await sseFetch(`/api/sessions/${state.session.id}/drafts/${did}/polish`, {}, (ev) => {
      if (ev.t === "delta") {
        box.querySelector(".stream-text").textContent += ev.v;
        box.scrollTop = box.scrollHeight;
      } else if (ev.t === "error") {
        throw new Error(ev.v);
      }
    });
    await refreshSessionData();
    toast("打磨完成 💎");
  } catch (e) {
    toast("打磨失败：" + e.message, true);
  } finally {
    box.remove();
    state.draftBusy = false;
  }
}

// ───────────────────────── 小结 ─────────────────────────
async function makeSummary() {
  if (!state.session) return;
  if (!state.session.messages.length) { toast("还没有讨论内容", true); return; }
  showModal("#modalSummary");
  $("#summaryBody").textContent = "生成中…";
  try {
    const r = await apiJson(`/api/sessions/${state.session.id}/summary`, { method: "POST" });
    $("#summaryBody").innerHTML = renderLightMd(r.summary.text);
    setTimeout(refreshFeishu, 2500);
  } catch (e) {
    $("#summaryBody").textContent = "生成失败：" + e.message;
  }
}

// 极简 markdown：## 标题 / - 列表 / 其余原样
function renderLightMd(text) {
  const out = [];
  for (const line of (text || "").split("\n")) {
    if (/^#{1,3}\s/.test(line)) out.push(`<h6>${escapeHtml(line.replace(/^#{1,3}\s/, ""))}</h6>`);
    else if (/^\s*[-•]\s/.test(line)) out.push(`· ${escapeHtml(line.replace(/^\s*[-•]\s/, ""))}`);
    else out.push(escapeHtml(line));
  }
  return out.join("<br>");
}

// ───────────────────────── 灵感库（全局） ─────────────────────────
let libData = { sparks: [], total: 0 };

async function loadLibrary() {
  const q = $("#libSearch").value.trim();
  libData = await apiJson(`/api/sparks/all${q ? `?q=${encodeURIComponent(q)}` : ""}`);
  renderLibrary();
}

function renderLibrary() {
  const cats = (state.config && state.config.topic_categories) || {};
  const list = $("#libList");
  $("#libTotal").textContent = `共 ${libData.total} 条`;
  if (!libData.sparks.length) {
    list.innerHTML = `<div class="tab-empty">${libData.total ? "没有匹配的灵感" : "灵感库还是空的<br>在各话题里收下 💡 后会汇总到这里"}</div>`;
    return;
  }
  list.innerHTML = "";
  for (const sp of libData.sparks) {
    const card = document.createElement("div");
    card.className = "spark-card lib-card";
    const catChip = sp.category
      ? `<span class="cat-mini" data-cat="${sp.category}">${cats[sp.category] || sp.category}</span>` : "";
    card.innerHTML = `
      <div class="lib-source" title="跳到该话题">${catChip}<span>📂 ${escapeHtml(sp.session_title)}</span></div>
      <div class="spark-content">${escapeHtml(sp.text)}</div>
      ${sp.note ? `<div class="spark-note">※ ${escapeHtml(sp.note)}</div>` : ""}`;
    card.querySelector(".lib-source").onclick = () => openSession(sp.session_id);
    list.appendChild(card);
  }
}

// ───────────────────────── 灵感碰撞器 ─────────────────────────
async function runCollide() {
  const body = $("#collideBody");
  $("#btnCollideAgain").hidden = true;
  body.innerHTML = `<div class="collide-loading">从灵感库里随机抽几张卡片，找它们之间的隐秘关联…</div>`;
  showModal("#modalCollide");
  try {
    const r = await apiJson("/api/sparks/collide", {
      method: "POST", body: JSON.stringify({ count: 3 }),
    });
    const html = [`<div class="collide-picked">🎴 本次抽到：<br>${r.picked.map(p => "· " + escapeHtml(p)).join("<br>")}</div>`];
    if (r.connections) {
      html.push(`<div class="collide-conn">⚡ <b>隐秘关联：</b>${escapeHtml(r.connections)}</div>`);
    }
    for (const d of r.directions) {
      html.push(`<div class="direction-card">
        <h5>${escapeHtml(d.title)}</h5>
        <div class="dir-why">${escapeHtml(d.why)}</div>
        <div class="dir-hook">「${escapeHtml(d.hook)}」</div>
        <button class="btn btn-primary" data-title="${escapeHtml(d.title)}" data-hook="${escapeHtml(d.hook)}">🚀 就聊这个</button>
      </div>`);
    }
    body.innerHTML = html.join("");
    body.querySelectorAll("[data-title]").forEach(btn => {
      btn.onclick = () => startFromDirection(btn.dataset.title, btn.dataset.hook);
    });
    $("#btnCollideAgain").hidden = false;
  } catch (e) {
    body.innerHTML = `<div class="collide-loading" style="color:var(--danger)">${escapeHtml(e.message)}</div>`;
    $("#btnCollideAgain").hidden = false;
  }
}

async function startFromDirection(title, hook) {
  try {
    const s = await apiJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title, mode: "brainstorm", seed: hook }),
    });
    hideModal("#modalCollide");
    await loadSessions();
    await openSession(s.id);
    toast("新话题已开，搭档先开了个头 ✍️");
    $("#inputBox").focus();
  } catch (e) { toast(e.message, true); }
}

// ───────────────────────── 本周写作统计 ─────────────────────────
function renderWeekStats() {
  const box = $("#weekStats");
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  const week = state.sessions.filter(s => (s.created_at || 0) * 1000 >= monday.getTime());
  const chars = week.reduce((a, s) => a + (s.chars || 0), 0);
  const sparks = state.sessions.reduce((a, s) => a + (s.spark_count || 0), 0);
  const drafts = state.sessions.reduce((a, s) => a + (s.draft_count || 0), 0);
  if (!state.sessions.length) { box.hidden = true; return; }
  const stat = (b, label) => `<div class="stat"><b>${b}</b><span>${label}</span></div>`;
  box.innerHTML =
    stat(week.length, "本周新话题") +
    stat(sparks, "累计灵感") +
    stat(drafts, "累计文案") +
    stat(chars >= 1000 ? (chars / 1000).toFixed(1) + "k" : chars, "本周产出字数");
  box.hidden = false;
}

// ───────────────────────── 飞书 ─────────────────────────
async function refreshFeishu() {
  if (!state.config) return;
  const el = $("#feishuState");
  try {
    const sid = state.session ? state.session.id : "";
    const r = await apiJson(`/api/feishu/status${sid ? `?sid=${sid}` : ""}`);
    $("#autoRecordToggle").checked = r.auto_record;
    if (!r.enabled) {
      el.textContent = "⚠️ 未配置（在 ⚙ 设置里填 App ID / Secret）";
    } else if (r.doc && r.doc.url) {
      el.textContent = r.auto_record ? "🟢 已连接，实时记录中" : "🟢 已连接（实时记录已关）";
      const a = $("#feishuDocLink");
      a.hidden = false;
      a.textContent = "📄 " + (state.session ? state.session.title : "话题文档");
      a.href = r.doc.url;
      const meta = $("#feishuMeta");
      meta.hidden = false;
      meta.textContent = `已记录 ${r.doc.turns_synced}/${r.doc.total_turns} 轮` +
        (r.backlog ? ` · 待写入 ${r.backlog} 组` : "");
    } else {
      el.textContent = r.auto_record ? "🟢 已连接，开聊后自动建文档" : "🟢 已连接（实时记录已关）";
      $("#feishuDocLink").hidden = true;
      $("#feishuMeta").hidden = true;
    }
    const err = r.last_error || (r.doc && r.doc.error) || "";
    const errEl = $("#feishuErr");
    errEl.hidden = !err;
    errEl.textContent = err ? "上次写入失败：" + err : "";
  } catch (e) {
    el.textContent = "状态获取失败：" + e.message;
  }
}

async function feishuCheck() {
  $("#btnFeishuCheck").disabled = true;
  toast("自检中…（会在飞书建一篇自检文档并删除）");
  try {
    const r = await apiJson("/api/feishu/check", { method: "POST" });
    toast((r.ok ? "✅ " : "❌ ") + r.msg, !r.ok);
  } catch (e) { toast(e.message, true); }
  finally { $("#btnFeishuCheck").disabled = false; }
}

// ───────────────────────── 素材精选（文案生成） ─────────────────────────
function renderSparkPicker() {
  const box = $("#sparkPicker");
  const sparks = state.session ? state.session.sparks : [];
  if (!sparks.length) {
    box.hidden = true;
    return;
  }
  const list = $("#pickerList");
  list.innerHTML = "";
  for (const sp of sparks) {
    const label = document.createElement("label");
    label.className = "picker-item";
    label.innerHTML = `<input type="checkbox" value="${sp.id}" checked><span>${escapeHtml(sp.text)}</span>`;
    list.appendChild(label);
  }
  box.hidden = false;
}

function pickedSparkIds() {
  if ($("#sparkPicker").hidden) return null;  // 未显示 = 不筛选，用全部
  return [...$("#pickerList input:checked")].map(i => i.value);
}

// ───────────────────────── 语音输入（浏览器支持才显示） ─────────────────────────
let micRecognition = null;
function setupMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;  // 不支持则按钮保持隐藏
  $("#btnMic").hidden = false;
  const btn = $("#btnMic");
  micRecognition = new SR();
  micRecognition.lang = "zh-CN";
  micRecognition.interimResults = false;
  micRecognition.maxAlternatives = 1;
  micRecognition.onresult = (e) => {
    const text = e.results[0][0].transcript;
    const box = $("#inputBox");
    box.value = (box.value ? box.value + " " : "") + text;
    box.focus();
  };
  micRecognition.onend = () => btn.classList.remove("recording");
  micRecognition.onerror = () => { btn.classList.remove("recording"); toast("语音识别失败", true); };
  btn.onclick = () => {
    if (btn.classList.contains("recording")) {
      micRecognition.stop();
      return;
    }
    btn.classList.add("recording");
    try { micRecognition.start(); } catch (e) { btn.classList.remove("recording"); }
  };
}

// ───────────────────────── 弹窗通用 ─────────────────────────
function showModal(sel) { $(sel).hidden = false; }
function hideModal(sel) { $(sel).hidden = true; }

// ───────────────────────── 设置 ─────────────────────────
async function openSettings() {
  const c = state.config;
  $("#cfgBaseUrl").value = c.llm.base_url;
  $("#cfgModel").value = c.llm.model;
  $("#cfgDraftModel").value = c.llm.draft_model;
  $("#cfgBackupUrl").value = c.llm.backup_base_url;
  $("#cfgBackupModel").value = c.llm.backup_model;
  $("#cfgApiKey").value = ""; $("#cfgApiKey").placeholder = c.llm.has_key ? "已保存（不回显）" : "sk-…";
  $("#cfgBackupKey").value = ""; $("#cfgBackupKey").placeholder = c.llm.has_backup_key ? "已保存（不回显）" : "可留空";
  $("#cfgFsAppId").value = c.feishu.app_id;
  $("#cfgFsFolder").value = c.feishu.folder_token;
  $("#cfgFsSecret").value = ""; $("#cfgFsSecret").placeholder = c.feishu.enabled ? "已保存（不回显）" : "App Secret";
  showModal("#modalSettings");
  // 写作画像（异步加载，不阻塞弹窗）
  try {
    const p = await apiJson("/api/profile");
    $("#profileText").value = p.profile || "";
    $("#profileText").placeholder = p.profile ? "" : "聊几轮后自动生成；也可点「立即更新」";
  } catch (e) { /* 忽略 */ }
}

async function saveSettings() {
  const llm = {
    base_url: $("#cfgBaseUrl").value.trim(),
    model: $("#cfgModel").value.trim(),
    draft_model: $("#cfgDraftModel").value.trim(),
    backup_base_url: $("#cfgBackupUrl").value.trim(),
    backup_model: $("#cfgBackupModel").value.trim(),
  };
  if ($("#cfgApiKey").value.trim()) llm.api_key = $("#cfgApiKey").value.trim();
  if ($("#cfgBackupKey").value.trim()) llm.backup_api_key = $("#cfgBackupKey").value.trim();
  const feishuCfg = {
    app_id: $("#cfgFsAppId").value.trim(),
    folder_token: $("#cfgFsFolder").value.trim(),
  };
  if ($("#cfgFsSecret").value.trim()) feishuCfg.app_secret = $("#cfgFsSecret").value.trim();
  try {
    await api("/api/config", { method: "POST", body: JSON.stringify({ llm, feishu: feishuCfg }) });
    state.config = await apiJson("/api/config");
    hideModal("#modalSettings");
    toast("设置已保存 ✅");
    refreshFeishu();
  } catch (e) { toast(e.message, true); }
}

// ───────────────────────── 新话题 ─────────────────────────
let newTopicMode = "free";
const MODE_DESC = {
  free: ["自由聊", "正常搭档：回应、追问、贡献角度"],
  brainstorm: ["头脑风暴", "发散为主，每轮给新方向"],
  deepdive: ["深挖追问", "一次盯一个点往深打"],
  challenge: ["唱反调", "专挑漏洞，逼你想清楚"],
};

function renderModeCards() {
  const box = $("#modeCards");
  box.innerHTML = "";
  for (const [k, [name, desc]] of Object.entries(MODE_DESC)) {
    const card = document.createElement("div");
    card.className = "mode-card" + (k === newTopicMode ? " active" : "");
    card.innerHTML = `<h5>${name}</h5><p>${desc}</p>`;
    card.onclick = () => {
      newTopicMode = k;
      $$("#modeCards .mode-card").forEach((c) => c.classList.remove("active"));
      card.classList.add("active");
    };
    box.appendChild(card);
  }
}

async function createTopic() {
  const title = $("#newTopicTitle").value.trim();
  try {
    const s = await apiJson("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title, mode: newTopicMode }),
    });
    hideModal("#modalNew");
    $("#newTopicTitle").value = "";
    await loadSessions();
    await openSession(s.id);
    toast("话题已建，开聊 ✍️");
    $("#inputBox").focus();
  } catch (e) { toast(e.message, true); }
}

// ───────────────────────── 初始化 ─────────────────────────
async function init() {
  state.config = await apiJson("/api/config");

  // 模式选择器（话题栏）
  const modeSel = $("#modeSelect");
  for (const [k, v] of Object.entries(state.config.modes)) {
    const o = document.createElement("option");
    o.value = k; o.textContent = v;
    modeSel.appendChild(o);
  }
  modeSel.onchange = async () => {
    if (!state.session) return;
    try {
      state.session = await apiJson(`/api/sessions/${state.session.id}`, {
        method: "PATCH",
        body: JSON.stringify({ mode: modeSel.value }),
      });
      toast(`已切换到「${state.config.modes[modeSel.value]}」`);
    } catch (e) { toast(e.message, true); }
  };

  fillDraftOptions();
  renderModeCards();
  await loadSessions(true);
  renderWeekStats();
  setupMic();

  // ── 事件绑定 ──
  $("#btnNewTopic").onclick = () => showModal("#modalNew");
  $("#btnCreateTopic").onclick = createTopic;
  $("#newTopicTitle").onkeydown = (e) => { if (e.key === "Enter") createTopic(); };

  $("#sessionSelect").onchange = (e) => e.target.value && openSession(e.target.value);
  $("#categoryFilter").onchange = () => renderSessionSelect();
  $("#btnAnalyze").onclick = runAnalysis;

  $("#btnSend").onclick = () => {
    const t = $("#inputBox").value.trim();
    if (t) { $("#inputBox").value = ""; sendMessage(t); }
  };
  $("#inputBox").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#btnSend").click();
    }
  };
  $$(".chip, .chip-suggest").forEach((c) => {
    c.onclick = () => {
      $("#inputBox").value = c.dataset.fill;
      $("#inputBox").focus();
    };
  });

  $("#topicTitle").onclick = async () => {
    if (!state.session) return;
    const t = prompt("新话题名：", state.session.title);
    if (t && t.trim() && t.trim() !== state.session.title) {
      try {
        state.session = await apiJson(`/api/sessions/${state.session.id}`, {
          method: "PATCH",
          body: JSON.stringify({ title: t.trim() }),
        });
        renderSession();
        await loadSessions();
      } catch (e) { toast(e.message, true); }
    }
  };

  $("#btnDeleteSession").onclick = async () => {
    if (!state.session) return;
    if (!confirm(`删除话题「${state.session.title}」？本地记录会消失（飞书文档保留）。`)) return;
    try {
      await api(`/api/sessions/${state.session.id}`, { method: "DELETE" });
      state.session = null;
      $("#chatList").innerHTML = "";
      $("#topicTitle").textContent = "（选择或新建一个话题）";
      renderSparks(); renderDrafts();
      await loadSessions();
      if (state.sessions.length) await openSession(state.sessions[0].id);
      refreshFeishu();
    } catch (e) { toast(e.message, true); }
  };

  $("#btnSummary").onclick = makeSummary;
  $("#btnCopySummary").onclick = async () => {
    await navigator.clipboard.writeText($("#summaryBody").innerText);
    toast("已复制");
  };

  $("#btnAddSpark").onclick = () => {
    if (!state.session) { toast("先新建一个话题", true); return; }
    openSparkModal();
  };
  $("#btnSaveSpark").onclick = saveSparkModal;

  $("#btnNewDraft").onclick = () => {
    if (!state.session) { toast("先新建一个话题", true); return; }
    $("#draftComposer").hidden = !$("#draftComposer").hidden;
    renderRecRow();
    renderSparkPicker();
  };
  $("#btnGenDraft").onclick = generateDraft;

  $("#btnSettings").onclick = openSettings;
  $("#btnSaveSettings").onclick = saveSettings;

  $$(".tab").forEach((tab) => {
    tab.onclick = () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      $$(".tab-body").forEach((b) => b.classList.remove("active"));
      tab.classList.add("active");
      $(`#tab-${tab.dataset.tab}`).classList.add("active");
      if (tab.dataset.tab === "feishu") refreshFeishu();
      if (tab.dataset.tab === "library") loadLibrary();
    };
  });

  // 灵感库 & 碰撞器
  $("#libSearch").oninput = () => {
    clearTimeout($("#libSearch")._timer);
    $("#libSearch")._timer = setTimeout(loadLibrary, 350);
  };
  $("#btnCollide").onclick = runCollide;
  $("#btnCollideAgain").onclick = runCollide;

  // 素材精选全选/全不选
  $("#pickerAll").onclick = () => $$("#pickerList input").forEach(i => (i.checked = true));
  $("#pickerNone").onclick = () => $$("#pickerList input").forEach(i => (i.checked = false));

  // 写作画像刷新
  $("#btnProfileRefresh").onclick = async () => {
    $("#btnProfileRefresh").disabled = true;
    try {
      const r = await apiJson("/api/profile/refresh", { method: "POST" });
      $("#profileText").value = r.profile;
      toast("写作画像已更新 ✍️");
    } catch (e) { toast(e.message, true); }
    finally { $("#btnProfileRefresh").disabled = false; }
  };

  $$(".modal-mask").forEach((mask) => {
    mask.addEventListener("click", (e) => { if (e.target === mask) mask.hidden = true; });
    mask.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => (mask.hidden = true)));
  });

  $("#autoRecordToggle").onchange = async (e) => {
    try {
      await api("/api/feishu/toggle", {
        method: "POST",
        body: JSON.stringify({ auto_record: e.target.checked }),
      });
      state.config.feishu.auto_record = e.target.checked;
      toast(e.target.checked ? "实时记录已开 🟢" : "实时记录已关");
      refreshFeishu();
    } catch (err) { toast(err.message, true); }
  };
  $("#btnFeishuCheck").onclick = feishuCheck;
  $("#btnFeishuSync").onclick = async () => {
    if (!state.session) { toast("先新建一个话题", true); return; }
    try {
      const r = await apiJson(`/api/sessions/${state.session.id}/feishu/resync`, { method: "POST" });
      toast(r.url ? "已安排补同步 ✅" : "已安排同步 ✅");
      if (r.url) window.open(r.url, "_blank");
      setTimeout(refreshFeishu, 2000);
    } catch (e) { toast(e.message, true); }
  };

  // 飞书面板周期刷新（仅在可见时）
  setInterval(() => {
    if ($("#tab-feishu").classList.contains("active")) refreshFeishu();
  }, 10000);

  refreshFeishu();
}

init().catch((e) => {
  document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif">
    加载失败：${escapeHtml(e.message)}<br><br>请确认服务已启动（python app.py）</div>`;
});
