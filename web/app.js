/* 打击乐节奏创作工具前端：控件由内核配置驱动，失败反馈回打击乐。 */
"use strict";

const state = {
  config: null,
  presets: {},
  catalog: [],
  candidates: [],
  warnings: [],
  refId: null,
  chosenId: null,
  edited: null,
  selected: -1,
  variation: {
    protoEvents: null,
    protoLabel: "",
    bars: [],
    voices: [],
    landing: [],
    result: null,
  },
  play: { ctx: null, nodes: [], raf: 0, start: 0, duration: 0, canvas: null, drawInfo: null },
};

const VOICE_COLORS = {
  kick: "#e06c5c", snare: "#4da3ff", hat_closed: "#d9c53b", hat_open: "#8fd14f",
  tom_hi: "#b07fe0", tom_lo: "#7f5ce0", rim: "#4fc3b0", clap: "#e08fc0",
};

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(data.message || data.error || `HTTP ${r.status}`);
  return data;
}

function stepMs() {
  return 60000 / state.config.bpm / (state.config.steps_per_bar / 4);
}
function totalSteps() {
  return state.config.bars * state.config.steps_per_bar;
}
function enabledVoices() {
  return state.config.voices.filter(v => v.enabled);
}

/* ---------------- 控件构建 ---------------- */

const GLOBAL_FIELDS = [
  ["bpm", "BPM", 30, 300, 1],
  ["bars", "小节数", 1, 4, 1],
  ["steps_per_bar", "每小节步数", 4, 32, 1],
  ["swing", "摇摆量", 0, 1, 0.01],
  ["max_shift_ms", "时移上限 ms", 0, 60, 1],
  ["timing_jitter_ms", "时移抖动 ms", 0, 30, 0.5],
  ["velocity_jitter", "力度抖动", 0, 0.5, 0.01],
  ["hand_min_interval", "双手最短间隔(步)", 0, 8, 1],
  ["n_candidates", "候选数", 1, 5, 1],
  ["sync_weight", "切分权重", 0, 1, 0.05],
];

function buildGlobalControls() {
  const box = document.getElementById("global-controls");
  box.innerHTML = "";
  for (const [key, label, min, max, step] of GLOBAL_FIELDS) {
    const lab = document.createElement("label");
    lab.textContent = label;
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = min; inp.max = max; inp.step = step;
    inp.value = state.config[key];
    inp.onchange = () => {
      state.config[key] = parseFloat(inp.value);
      if (["bars", "steps_per_bar"].includes(key)) {
        pruneAccents();
        buildVoices();
      }
    };
    lab.appendChild(inp);
    box.appendChild(lab);
  }
}

function pruneAccents() {
  const n = totalSteps();
  for (const v of state.config.voices) {
    for (const k of Object.keys(v.accents)) {
      if (parseInt(k) >= n) delete v.accents[k];
    }
  }
}

function buildVoices() {
  const box = document.getElementById("voices");
  box.innerHTML = "";
  const n = totalSteps();
  const spb = state.config.steps_per_bar;
  for (const v of state.config.voices) {
    const row = document.createElement("div");
    row.className = "voice-row";
    row.dataset.voice = v.name;

    const head = document.createElement("div");
    head.className = "voice-head";
    head.innerHTML = `
      <label><input type="checkbox" class="v-enabled" ${v.enabled ? "checked" : ""}> 启用</label>
      <span class="name">${v.name}</span>
      <label>密度 <input type="number" class="v-density" min="0" max="${n}" value="${v.density}"></label>
      <label>最短间隔 <input type="number" class="v-minint" min="1" max="${n}" value="${v.min_interval}"></label>
      <label><input type="checkbox" class="v-hand" ${v.hand ? "checked" : ""}> 双手组</label>
      <label>基准力度 <input type="number" class="v-vel" min="0.05" max="1" step="0.05" value="${v.base_velocity}"></label>`;
    row.appendChild(head);

    const strip = document.createElement("div");
    strip.className = "accent-strip";
    for (let s = 0; s < n; s++) {
      const cell = document.createElement("div");
      cell.className = "accent-cell" + (s % spb === 0 && s > 0 ? " bar-start" : "");
      const lv = v.accents[String(s)] || 0;
      if (lv === 1) cell.classList.add("soft");
      if (lv === 2) cell.classList.add("locked");
      cell.title = `第${s}步`;
      cell.onclick = () => {
        const cur = v.accents[String(s)] || 0;
        const nxt = (cur + 1) % 3;
        if (nxt === 0) delete v.accents[String(s)];
        else v.accents[String(s)] = nxt;
        cell.className = "accent-cell" + (s % spb === 0 && s > 0 ? " bar-start" : "") +
          (nxt === 1 ? " soft" : nxt === 2 ? " locked" : "");
      };
      strip.appendChild(cell);
    }
    row.appendChild(strip);
    box.appendChild(row);

    row.querySelector(".v-enabled").onchange = e => { v.enabled = e.target.checked; };
    row.querySelector(".v-density").onchange = e => { v.density = parseInt(e.target.value) || 0; };
    row.querySelector(".v-minint").onchange = e => { v.min_interval = Math.max(1, parseInt(e.target.value) || 1); };
    row.querySelector(".v-hand").onchange = e => { v.hand = e.target.checked; };
    row.querySelector(".v-vel").onchange = e => { v.base_velocity = parseFloat(e.target.value); };
  }
}

function buildRelations() {
  const box = document.getElementById("relations");
  box.innerHTML = "";
  state.config.relations.forEach((rel, i) => {
    const row = document.createElement("div");
    row.className = "relation-row";
    const desc = rel.kind === "answer"
      ? `呼应 ${rel.a} → ${rel.b}（+${rel.offset}步）`
      : rel.kind === "unison"
        ? `齐奏 ${rel.a} ⇒ ${rel.b}`
        : `互斥 ${rel.a} ⊥ ${rel.b}（窗口 ${rel.window} 步）`;
    row.innerHTML = `<span>${desc}</span>`;
    const del = document.createElement("button");
    del.textContent = "删除";
    del.className = "danger";
    del.onclick = () => { state.config.relations.splice(i, 1); buildRelations(); };
    row.appendChild(del);
    box.appendChild(row);
  });

  const add = document.getElementById("relation-add");
  add.innerHTML = "";
  const kind = document.createElement("select");
  kind.innerHTML = `<option value="exclude">互斥</option><option value="unison">齐奏</option><option value="answer">呼应</option>`;
  const names = state.config.voices.map(v => v.name);
  const selA = document.createElement("select");
  const selB = document.createElement("select");
  for (const nm of names) {
    selA.add(new Option(nm, nm));
    selB.add(new Option(nm, nm));
  }
  selB.selectedIndex = Math.min(1, names.length - 1);
  const off = document.createElement("input");
  off.type = "number"; off.min = 0; off.max = totalSteps() - 1; off.value = 4; off.title = "呼应步数";
  const win = document.createElement("input");
  win.type = "number"; win.min = 0; win.max = 4; win.value = 0; win.title = "互斥窗口(步)";
  const btn = document.createElement("button");
  btn.textContent = "添加关系";
  btn.onclick = () => {
    if (selA.value === selB.value) { setStatus("关系的两个声部不能相同", "error"); return; }
    state.config.relations.push({
      kind: kind.value, a: selA.value, b: selB.value,
      offset: parseInt(off.value) || 0, window: parseInt(win.value) || 0,
    });
    buildRelations();
  };
  for (const el of [kind, selA, selB,
    document.createTextNode("呼应+"), off,
    document.createTextNode("窗口"), win, btn]) add.appendChild(el);
}

/* ---------------- 生成与结果 ---------------- */

function setStatus(msg, cls = "") {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = cls;
}

async function generate() {
  state.config.seed = parseInt(document.getElementById("seed").value) || 0;
  stopAudio();
  setStatus("求解中…");
  document.getElementById("failure").classList.add("hidden");
  document.getElementById("candidates").innerHTML = "";
  document.getElementById("warnings").innerHTML = "";
  document.getElementById("editor-panel").classList.add("hidden");
  document.getElementById("variation-panel").classList.add("hidden");
  state.variation.protoEvents = null;
  state.variation.result = null;
  document.querySelectorAll(".voice-row").forEach(r => r.classList.remove("conflict"));
  let resp;
  try {
    resp = await api("/api/generate", { config: state.config });
  } catch (e) {
    setStatus("请求失败：" + e.message, "error");
    return;
  }
  if (resp.status === "ok") {
    state.candidates = resp.candidates;
    state.warnings = resp.warnings || [];
    state.refId = null;
    setStatus(`求解成功：${resp.candidates.length} 个候选（种子 ${resp.seed}，可复算）`, "ok");
    renderWarnings();
    renderCandidates();
  } else {
    state.candidates = [];
    renderFailure(resp);
  }
}

function renderWarnings() {
  const box = document.getElementById("warnings");
  box.innerHTML = "";
  for (const w of state.warnings) {
    const d = document.createElement("div");
    d.textContent = "⚠ " + w;
    box.appendChild(d);
  }
}

function renderFailure(resp) {
  setStatus(resp.status === "unsat" ? "约束不可满足" : "求解未完成", "error");
  const box = document.getElementById("failure");
  box.classList.remove("hidden");
  const items = (resp.core || []).map(c => `<li><code>${c.id}</code> ${c.text}</li>`).join("");
  box.innerHTML = `
    <h3>${resp.message}</h3>
    <div>最小锁定集合（这些锁定条件放在一起必然冲突，移除任一即可恢复可行）：</div>
    <ul>${items || "<li>（未能定位到具体锁定子集）</li>"}</ul>
    <div class="note">未自动降低任何密度/间隔/关系要求；请调整上述条件后重新生成。</div>`;
  // 高亮涉及声部
  for (const c of resp.core || []) {
    const m = c.id.match(/:(\w+)(@|$)/);
    if (m) {
      const row = document.querySelector(`.voice-row[data-voice="${m[1]}"]`);
      if (row) row.classList.add("conflict");
    }
  }
}

function eventSet(events) {
  return new Set(events.map(e => `${e.voice}:${e.step}`));
}

function renderCandidates() {
  const box = document.getElementById("candidates");
  box.innerHTML = "";
  state.candidates.forEach((cand, i) => {
    const card = document.createElement("div");
    card.className = "candidate";
    card.dataset.id = cand.id;

    const head = document.createElement("div");
    head.className = "cand-head";
    head.innerHTML = `
      <span class="cid">候选 #${cand.id + 1}</span>
      <div class="scores">
        切分 <b>${cand.scores.syncopation}</b> ·
        重复 <b>${cand.scores.repetition}</b> ·
        综合 <b>${cand.scores.total}</b>
      </div>
      <div class="cand-actions">
        <button class="b-play">播放</button>
        <button class="b-stop">停止</button>
        <button class="b-ref">设为基准</button>
        <button class="b-edit">选择并编辑</button>
        <button class="b-proto">作为变奏原型</button>
      </div>`;
    card.appendChild(head);

    const solo = document.createElement("div");
    solo.className = "solo-row";
    for (const v of enabledVoices()) {
      const lab = document.createElement("label");
      lab.innerHTML = `<input type="checkbox" data-voice="${v.name}" checked> ${v.name}`;
      solo.appendChild(lab);
    }
    card.appendChild(solo);

    const canvas = document.createElement("canvas");
    canvas.className = "lanes";
    card.appendChild(canvas);
    drawLanes(canvas, cand.events, {});

    const diffLab = document.createElement("label");
    diffLab.className = "hint";
    diffLab.innerHTML = `<input type="checkbox" class="diff-toggle"> 与基准候选对比差异`;
    diffLab.querySelector(".diff-toggle").onchange = e => {
      redrawWithDiff(canvas, cand, e.target.checked);
    };
    card.appendChild(diffLab);

    if (cand.repairs && cand.repairs.length) {
      const r = document.createElement("div");
      r.className = "repairs";
      r.innerHTML = "修复回退：" + cand.repairs.map(x => `<div>· ${x}</div>`).join("");
      card.appendChild(r);
    }
    if (cand.adjustments && cand.adjustments.length) {
      const a = document.createElement("div");
      a.className = "adjusts";
      a.innerHTML = "实际时间终检修正：" + cand.adjustments.map(x => `<div>· ${x}</div>`).join("");
      card.appendChild(a);
    }

    const expl = document.createElement("details");
    expl.className = "expl";
    expl.innerHTML = `<summary>约束解释（${cand.explanations.length} 条）</summary>` +
      "<ul>" + cand.explanations.map(x => `<li>${x}</li>`).join("") + "</ul>";
    card.appendChild(expl);

    const table = document.createElement("details");
    table.className = "expl";
    table.innerHTML = `<summary>打击事件与实际演奏时间（${cand.events.length} 击）</summary>` +
      eventTable(cand.events);
    card.appendChild(table);

    head.querySelector(".b-play").onclick = () => playEvents(cand.events, canvas, solo, cand);
    head.querySelector(".b-stop").onclick = stopAudio;
    head.querySelector(".b-ref").onclick = () => {
      state.refId = cand.id;
      document.querySelectorAll(".candidate").forEach(c => c.classList.remove("chosen"));
      card.classList.add("chosen");
      setStatus(`候选 #${cand.id + 1} 已设为对比基准`, "ok");
    };
    head.querySelector(".b-edit").onclick = () => chooseForEdit(cand.id);
    head.querySelector(".b-proto").onclick = () => choosePrototype(cand.id);

    box.appendChild(card);
  });
}

function eventTable(events) {
  const rows = events.map(e =>
    `<tr><td>${e.voice}</td><td>${e.step}</td><td>${e.time_ms.toFixed(1)}</td>` +
    `<td>${e.shift_ms.toFixed(1)}</td><td>${e.velocity.toFixed(2)}</td></tr>`).join("");
  return `<table class="event-table"><tr><th>声部</th><th>步</th><th>实际ms</th><th>时移ms</th><th>力度</th></tr>${rows}</table>`;
}

function redrawWithDiff(canvas, cand, on) {
  if (!on || state.refId === null || state.refId === cand.id) {
    drawLanes(canvas, cand.events, {});
    return;
  }
  const ref = state.candidates.find(c => c.id === state.refId);
  const sRef = eventSet(ref.events), sC = eventSet(cand.events);
  const onlyHere = new Set([...sC].filter(x => !sRef.has(x)));
  const onlyRef = new Set([...sRef].filter(x => !sC.has(x)));
  const ghosts = ref.events.filter(e => onlyRef.has(`${e.voice}:${e.step}`));
  drawLanes(canvas, cand.events, { onlyHere, ghosts });
}

/* ---------------- 车道绘制 ---------------- */

function laneGeometry() {
  const voices = enabledVoices();
  const n = totalSteps();
  const cellW = Math.max(14, Math.min(24, Math.floor(920 / n)));
  const cellH = 26, labelW = 86, pad = 6;
  return { voices, n, cellW, cellH, labelW, pad,
           W: labelW + n * cellW + pad * 2, H: voices.length * cellH + pad * 2 };
}

function drawLanes(canvas, events, opts) {
  const g = laneGeometry();
  canvas.width = g.W; canvas.height = g.H;
  const ctx = canvas.getContext("2d");
  const spb = state.config.steps_per_bar;
  const q = Math.max(1, spb / 4);
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, g.W, g.H);
  // 强拍底纹
  for (let s = 0; s < g.n; s++) {
    if (s % q === 0) {
      ctx.fillStyle = (s % spb === 0) ? "rgba(255,255,255,0.055)" : "rgba(255,255,255,0.03)";
      ctx.fillRect(g.labelW + s * g.cellW, g.pad, g.cellW, g.H - g.pad * 2);
    }
  }
  g.voices.forEach((v, r) => {
    const y = g.pad + r * g.cellH;
    ctx.fillStyle = "#8b919c";
    ctx.font = "11px sans-serif";
    ctx.fillText(v.name, 6, y + g.cellH / 2 + 4);
    ctx.strokeStyle = "#262a31";
    ctx.strokeRect(g.labelW, y, g.n * g.cellW, g.cellH);
    for (let s = 1; s < g.n; s++) {
      ctx.beginPath();
      ctx.moveTo(g.labelW + s * g.cellW, y);
      ctx.lineTo(g.labelW + s * g.cellW, y + g.cellH);
      ctx.strokeStyle = s % spb === 0 ? "#3a3f48" : "#1c1f25";
      ctx.stroke();
    }
  });
  const sms = stepMs();
  for (const e of events) {
    const r = g.voices.findIndex(v => v.name === e.voice);
    if (r < 0) continue;
    const key = `${e.voice}:${e.step}`;
    const cx = g.labelW + e.step * g.cellW + g.cellW / 2 + (e.shift_ms / sms) * g.cellW;
    const h = 5 + e.velocity * (g.cellH - 9);
    const y = g.pad + r * g.cellH;
    ctx.fillStyle = VOICE_COLORS[e.voice] || "#ccc";
    ctx.fillRect(cx - g.cellW * 0.32, y + g.cellH - 3 - h, g.cellW * 0.64, h);
    if (e.accent === 2) {
      ctx.strokeStyle = "#ffb020";
      ctx.lineWidth = 2;
      ctx.strokeRect(cx - g.cellW * 0.36, y + g.cellH - 4 - h, g.cellW * 0.72, h + 2);
      ctx.lineWidth = 1;
    }
    if (opts.onlyHere && opts.onlyHere.has(key)) {
      ctx.strokeStyle = "#4caf7d";
      ctx.lineWidth = 2;
      ctx.strokeRect(cx - g.cellW * 0.42, y + 2, g.cellW * 0.84, g.cellH - 4);
      ctx.lineWidth = 1;
    }
  }
  for (const e of (opts.ghosts || [])) {
    const r = g.voices.findIndex(v => v.name === e.voice);
    if (r < 0) continue;
    const cx = g.labelW + e.step * g.cellW + g.cellW / 2;
    const y = g.pad + r * g.cellH;
    ctx.strokeStyle = "#e05c5c";
    ctx.setLineDash([3, 3]);
    ctx.strokeRect(cx - g.cellW * 0.42, y + 2, g.cellW * 0.84, g.cellH - 4);
    ctx.setLineDash([]);
  }
  return g;
}

/* ---------------- 音频播放 ---------------- */

function ensureCtx() {
  if (!state.play.ctx) {
    state.play.ctx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return state.play.ctx;
}

async function fetchBuffer(url) {
  const ctx = ensureCtx();
  const ab = await (await fetch(url)).arrayBuffer();
  return await ctx.decodeAudioData(ab);
}

function stopAudio() {
  for (const n of state.play.nodes) { try { n.stop(); } catch (_) {} }
  state.play.nodes = [];
  cancelAnimationFrame(state.play.raf);
  state.play.canvas = null;
}

async function playEvents(events, canvas, soloRow, cand) {
  stopAudio();
  setStatus("渲染音频中…");
  let resp;
  try {
    resp = await api("/api/audition", { config: state.config, events });
  } catch (e) {
    setStatus("音频渲染失败：" + e.message, "error");
    return;
  }
  const ctx = ensureCtx();
  const active = soloRow
    ? [...soloRow.querySelectorAll("input:checked")].map(i => i.dataset.voice)
    : Object.keys(resp.stems);
  const urls = active.filter(v => resp.stems[v]).map(v => resp.stems[v]);
  const bufs = await Promise.all(urls.map(fetchBuffer));
  const t0 = ctx.currentTime + 0.08;
  for (const buf of bufs) {
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(t0);
    state.play.nodes.push(src);
  }
  state.play.start = t0;
  state.play.duration = resp.duration_ms / 1000;
  state.play.canvas = canvas;
  state.play.drawInfo = { events, opts: {} };
  animateCursor();
  setStatus(`播放中（${resp.duration_ms} ms，实际演奏时间为准）`, "ok");
}

function animateCursor() {
  const p = state.play;
  if (!p.canvas || !p.ctx) return;
  const now = p.ctx.currentTime;
  const pos = (now - p.start) / p.duration;
  const events = p.drawInfo.events;
  const g = drawLanes(p.canvas, events, p.drawInfo.opts);
  if (pos >= 0 && pos <= 1) {
    const x = g.labelW + pos * g.n * g.cellW;
    const ctx2 = p.canvas.getContext("2d");
    ctx2.strokeStyle = "#ffffff";
    ctx2.beginPath();
    ctx2.moveTo(x, 0);
    ctx2.lineTo(x, g.H);
    ctx2.stroke();
  }
  if (pos < 1) {
    p.raf = requestAnimationFrame(animateCursor);
  } else {
    stopAudio();
    setStatus("播放结束", "ok");
  }
}

/* ---------------- 编辑器 ---------------- */

function chooseForEdit(id) {
  const cand = state.candidates.find(c => c.id === id);
  if (!cand) return;
  state.chosenId = id;
  state.edited = cand.events.map(e => ({ ...e }));
  state.selected = -1;
  document.getElementById("editor-panel").classList.remove("hidden");
  document.getElementById("vel-slider").disabled = true;
  document.getElementById("selected-info").textContent = "";
  document.getElementById("editor-status").textContent = "";
  drawEditor();
  validateEdited();
  document.getElementById("editor-panel").scrollIntoView({ behavior: "smooth" });
}

function editorGeometry() {
  const g = laneGeometry();
  g.cellW = Math.max(18, g.cellW);
  g.cellH = 32;
  g.W = g.labelW + g.n * g.cellW + g.pad * 2;
  g.H = g.voices.length * g.cellH + g.pad * 2;
  return g;
}

function drawEditor() {
  const canvas = document.getElementById("editor-canvas");
  const g = editorGeometry();
  canvas.width = g.W; canvas.height = g.H;
  const ctx = canvas.getContext("2d");
  const spb = state.config.steps_per_bar;
  const q = Math.max(1, spb / 4);
  ctx.fillStyle = "#101216";
  ctx.fillRect(0, 0, g.W, g.H);
  for (let s = 0; s < g.n; s++) {
    if (s % q === 0) {
      ctx.fillStyle = s % spb === 0 ? "rgba(255,255,255,0.055)" : "rgba(255,255,255,0.03)";
      ctx.fillRect(g.labelW + s * g.cellW, g.pad, g.cellW, g.H - g.pad * 2);
    }
  }
  g.voices.forEach((v, r) => {
    const y = g.pad + r * g.cellH;
    ctx.fillStyle = "#8b919c";
    ctx.font = "11px sans-serif";
    ctx.fillText(v.name, 6, y + g.cellH / 2 + 4);
    ctx.strokeStyle = "#262a31";
    ctx.strokeRect(g.labelW, y, g.n * g.cellW, g.cellH);
    for (let s = 1; s < g.n; s++) {
      ctx.beginPath();
      ctx.moveTo(g.labelW + s * g.cellW, y);
      ctx.lineTo(g.labelW + s * g.cellW, y + g.cellH);
      ctx.strokeStyle = s % spb === 0 ? "#3a3f48" : "#1c1f25";
      ctx.stroke();
    }
    // 锁定格标记
    for (const [k, lv] of Object.entries(v.accents)) {
      if (lv === 2) {
        const s = parseInt(k);
        ctx.fillStyle = "rgba(255,176,32,0.18)";
        ctx.fillRect(g.labelW + s * g.cellW, y, g.cellW, g.cellH);
      }
    }
  });
  const sms = stepMs();
  state.edited.forEach((e, idx) => {
    const r = g.voices.findIndex(v => v.name === e.voice);
    if (r < 0) return;
    const cx = g.labelW + e.step * g.cellW + g.cellW / 2 + (e.shift_ms / sms) * g.cellW;
    const h = 6 + e.velocity * (g.cellH - 10);
    const y = g.pad + r * g.cellH;
    ctx.fillStyle = VOICE_COLORS[e.voice] || "#ccc";
    ctx.fillRect(cx - g.cellW * 0.32, y + g.cellH - 3 - h, g.cellW * 0.64, h);
    if (e.accent === 2) {
      ctx.strokeStyle = "#ffb020";
      ctx.lineWidth = 2;
      ctx.strokeRect(cx - g.cellW * 0.36, y + g.cellH - 4 - h, g.cellW * 0.72, h + 2);
      ctx.lineWidth = 1;
    }
    if (idx === state.selected) {
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 2;
      ctx.strokeRect(cx - g.cellW * 0.44, y + 1, g.cellW * 0.88, g.cellH - 2);
      ctx.lineWidth = 1;
    }
  });
}

function editorClick(ev) {
  const canvas = document.getElementById("editor-canvas");
  const rect = canvas.getBoundingClientRect();
  const g = editorGeometry();
  const x = (ev.clientX - rect.left) * (canvas.width / rect.width);
  const y = (ev.clientY - rect.top) * (canvas.height / rect.height);
  if (x < g.labelW) return;
  const step = Math.floor((x - g.labelW) / g.cellW);
  const row = Math.floor((y - g.pad) / g.cellH);
  if (step < 0 || step >= g.n || row < 0 || row >= g.voices.length) return;
  const voice = g.voices[row].name;
  const idx = state.edited.findIndex(e => e.voice === voice && e.step === step);
  if (idx >= 0) {
    state.selected = idx;
    const e = state.edited[idx];
    const slider = document.getElementById("vel-slider");
    slider.disabled = false;
    slider.value = e.velocity;
    document.getElementById("selected-info").textContent =
      `选中 ${e.voice}@${e.step}（力度 ${e.velocity.toFixed(2)}，时移 ${e.shift_ms.toFixed(1)}ms）`;
  } else {
    const v = state.config.voices.find(v => v.name === voice);
    const accent = v.accents[String(step)] || 0;
    state.edited.push({
      voice, step, velocity: v.base_velocity, shift_ms: 0, accent,
      time_ms: step * stepMs(),
    });
    state.edited.sort((a, b) => a.step - b.step || a.voice.localeCompare(b.voice));
    state.selected = state.edited.findIndex(e => e.voice === voice && e.step === step);
  }
  drawEditor();
  validateEdited();
}

function deleteSelected() {
  if (state.selected < 0) return;
  const e = state.edited[state.selected];
  if (e.accent === 2) {
    setEditorStatus("锁定重音不可删除（硬约束）", "error");
    return;
  }
  state.edited.splice(state.selected, 1);
  state.selected = -1;
  document.getElementById("vel-slider").disabled = true;
  document.getElementById("selected-info").textContent = "";
  drawEditor();
  validateEdited();
}

let validateTimer = 0;
async function validateEdited() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    try {
      const resp = await api("/api/validate", { config: state.config, events: state.edited });
      const el = document.getElementById("editor-status");
      if (resp.ok) {
        el.textContent = "✓ 硬约束全部满足（含实际演奏时间终检）";
        el.className = "ok";
      } else {
        el.textContent = "✗ " + resp.violations.join("；");
        el.className = "error";
      }
    } catch (e) {
      setEditorStatus("校验失败：" + e.message, "error");
    }
  }, 250);
}

function setEditorStatus(msg, cls = "") {
  const el = document.getElementById("editor-status");
  el.textContent = msg;
  el.className = cls;
}

async function auditionEdited() {
  stopAudio();
  setEditorStatus("渲染音频中…");
  try {
    const resp = await api("/api/audition", { config: state.config, events: state.edited });
    const ctx = ensureCtx();
    const urls = Object.values(resp.stems);
    const bufs = await Promise.all(urls.map(fetchBuffer));
    const t0 = ctx.currentTime + 0.08;
    for (const buf of bufs) {
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start(t0);
      state.play.nodes.push(src);
    }
    document.getElementById("editor-events").innerHTML =
      `<table class="event-table"><tr><th>声部</th><th>步</th><th>实际ms</th><th>力度</th></tr>` +
      resp.actual_events.map(e =>
        `<tr><td>${e.voice}</td><td>${e.step}</td><td>${e.time_ms.toFixed(1)}</td><td>${e.velocity.toFixed(2)}</td></tr>`).join("") +
      "</table>";
    setEditorStatus(`播放编辑结果（${resp.duration_ms} ms）`, "ok");
  } catch (e) {
    setEditorStatus("渲染失败：" + e.message, "error");
  }
}

/* ---------------- 保留主型的变奏与填充 ---------------- */

const VARIATION_FIELDS = [
  ["min_distance", "变奏距离下限", 0, 128, 1],
  ["max_distance", "变奏距离上限", 1, 128, 1],
  ["fill_end_budget", "末拍占用预算", 0, 64, 1],
  ["velocity_delta", "力度调整幅度", 0, 0.5, 0.02],
  ["disp_window", "位移配对窗口(步)", 1, 32, 1],
];

function choosePrototype(id) {
  const cand = state.candidates.find(c => c.id === id);
  if (!cand) return;
  const v = state.variation;
  v.protoEvents = cand.events.map(e => ({ ...e }));
  v.protoLabel = `候选 #${cand.id + 1}（${cand.events.length} 击）`;
  v.bars = [Math.max(0, state.config.bars - 1)];
  v.voices = enabledVoices().map(x => x.name);
  v.landing = [];
  v.result = null;
  buildVariationControls();
  document.getElementById("variation-panel").classList.remove("hidden");
  document.getElementById("variation-panel").scrollIntoView({ behavior: "smooth" });
  setVariationStatus(`原型已取 ${v.protoLabel}：区外事件与骨架将硬保留`, "ok");
}

function buildVariationControls() {
  const v = state.variation;
  const box = document.getElementById("variation-controls");
  box.innerHTML = "";

  const barLab = document.createElement("label");
  barLab.textContent = "变奏小节（多选）";
  const barBox = document.createElement("div");
  barBox.style.display = "flex";
  barBox.style.gap = "6px";
  for (let b = 0; b < state.config.bars; b++) {
    const lab = document.createElement("label");
    lab.style.color = "var(--text)";
    lab.innerHTML = `<input type="checkbox" class="v-bar" value="${b}" ${v.bars.includes(b) ? "checked" : ""}> 第${b + 1}小节`;
    lab.querySelector("input").onchange = e => {
      const bv = parseInt(e.target.value);
      v.bars = e.target.checked ? [...v.bars, bv].sort((a, c) => a - c)
        : v.bars.filter(x => x !== bv);
    };
    barBox.appendChild(lab);
  }
  barLab.appendChild(barBox);
  box.appendChild(barLab);

  for (const [key, label, min, max, step] of VARIATION_FIELDS) {
    const cur = v[key] !== undefined ? v[key] :
      (key === "min_distance" ? 2 : key === "max_distance" ? 12 :
       key === "fill_end_budget" ? 2 : key === "velocity_delta" ? 0.2 :
       Math.max(1, state.config.steps_per_bar / 4));
    v[key] = cur;
    const lab = document.createElement("label");
    lab.textContent = label;
    const inp = document.createElement("input");
    inp.type = "number"; inp.min = min; inp.max = max; inp.step = step; inp.value = cur;
    inp.onchange = () => { v[key] = parseFloat(inp.value); };
    lab.appendChild(inp);
    box.appendChild(lab);
  }

  const keepLab = document.createElement("label");
  keepLab.textContent = "关键重音";
  keepLab.innerHTML += `<label style="color:var(--text)"><input type="checkbox" class="v-keepsoft" checked> 软重音一并锁定</label>`;
  keepLab.querySelector(".v-keepsoft").onchange = e => { v.keepSoft = e.target.checked; };
  v.keepSoft = v.keepSoft !== false;
  box.appendChild(keepLab);

  const vbox = document.getElementById("variation-voices");
  vbox.innerHTML = "<span class='hint'>参与变奏声部：</span>";
  for (const voice of enabledVoices()) {
    const lab = document.createElement("label");
    lab.innerHTML = `<input type="checkbox" class="v-voice" value="${voice.name}" ${v.voices.includes(voice.name) ? "checked" : ""}> ${voice.name}`;
    lab.querySelector("input").onchange = e => {
      const nm = e.target.value;
      v.voices = e.target.checked ? [...v.voices, nm] : v.voices.filter(x => x !== nm);
    };
    vbox.appendChild(lab);
  }

  const lbox = document.getElementById("variation-landing");
  lbox.innerHTML = "<span class='hint'>乐句落点（进入下一乐句必须命中的事件）：</span> ";
  const selV = document.createElement("select");
  for (const voice of enabledVoices()) selV.add(new Option(voice.name, voice.name));
  const inpS = document.createElement("input");
  inpS.type = "number"; inpS.min = 0; inpS.max = totalSteps() - 1; inpS.placeholder = "步号";
  inpS.style.width = "80px";
  const addBtn = document.createElement("button");
  addBtn.textContent = "添加落点";
  addBtn.onclick = () => {
    const step = parseInt(inpS.value);
    if (Number.isNaN(step) || step < 0 || step >= totalSteps()) {
      setVariationStatus("落点步号越界", "error"); return;
    }
    if (v.landing.some(x => x.voice === selV.value && x.step === step)) return;
    v.landing.push({ voice: selV.value, step });
    inpS.value = "";
    renderLandingChips();
  };
  lbox.appendChild(selV); lbox.appendChild(inpS); lbox.appendChild(addBtn);
  renderLandingChips();

  document.getElementById("variation-proto-info").textContent =
    `原型：${v.protoLabel || "未选择"}`;
}

function renderLandingChips() {
  const lbox = document.getElementById("variation-landing");
  lbox.querySelectorAll(".chip").forEach(x => x.remove());
  for (const item of state.variation.landing) {
    const chip = document.createElement("span");
    chip.className = "chip landing";
    chip.textContent = `${item.voice}@${item.step} ✕`;
    chip.onclick = () => {
      state.variation.landing = state.variation.landing.filter(
        x => !(x.voice === item.voice && x.step === item.step));
      renderLandingChips();
    };
    lbox.appendChild(chip);
  }
}

function setVariationStatus(msg, cls = "") {
  const el = document.getElementById("variation-status");
  el.textContent = msg;
  el.className = cls;
}

async function runVariation() {
  const v = state.variation;
  if (!v.protoEvents) {
    setVariationStatus("请先在候选卡片上选择「作为变奏原型」", "error");
    return;
  }
  if (!v.bars.length) {
    setVariationStatus("至少选择一个变奏小节", "error");
    return;
  }
  if (!v.voices.length) {
    setVariationStatus("至少选择一个参与声部", "error");
    return;
  }
  if (v.min_distance > v.max_distance) {
    setVariationStatus("变奏距离下限不能大于上限", "error");
    return;
  }
  stopAudio();
  setVariationStatus("约束搜索变奏中（staged 与 joint 同空间对照）…");
  const payload = {
    config: state.config,
    prototype: v.protoEvents,
    variation: {
      bars: v.bars, voices: v.voices,
      min_distance: v.min_distance, max_distance: v.max_distance,
      fill_end_budget: v.fill_end_budget, velocity_delta: v.velocity_delta,
      disp_window: v.disp_window, keep_soft: v.keepSoft,
      landing: v.landing,
    },
  };
  let resp;
  try {
    resp = await api("/api/variate", payload);
  } catch (e) {
    setVariationStatus("变奏请求失败：" + e.message, "error");
    return;
  }
  if (resp.status !== "ok") {
    setVariationStatus(`${resp.message || "变奏预算内无接受候选"}：${(resp.reasons || []).join("；")}`, "error");
    document.getElementById("variation-results").innerHTML = "";
    return;
  }
  v.result = resp;
  const anyAccepted = Object.values(resp.strategies)
    .some(o => (o.accepted || []).length > 0);
  setVariationStatus(anyAccepted
    ? "变奏完成：接受/舍弃原因与逐项变化如下，可分别试听对照"
    : "预算内无接受候选：下列候选全部被舍弃，展开可查看原因并试听核对",
    anyAccepted ? "ok" : "error");
  renderVariationResult(resp);
}

function renderVariationResult(resp) {
  const skelBox = document.getElementById("variation-skeleton");
  skelBox.innerHTML = "<span class='hint'>硬保留骨架：</span>" +
    resp.skeleton.map(s => {
      const cls = s.reason.includes("锁定") ? "locked"
        : s.reason.includes("落点") ? "landing" : "soft";
      return `<span class="chip ${cls}" title="${s.reason}">${s.voice}@${s.step} · ${s.reason}</span>`;
    }).join("");

  const cmp = document.getElementById("variation-comparison");
  cmp.innerHTML = "<b>策略对照</b><ul>" +
    resp.comparison.lines.map(x => `<li>${x}</li>`).join("") + "</ul>" +
    `<div class="hint">优胜策略：${resp.comparison.winner === "tie" ? "平手" :
      (resp.comparison.winner === "joint" ? "约束搜索同时决定变奏与收束" : "先锁强拍再配填充")}</div>`;

  const box = document.getElementById("variation-results");
  box.innerHTML = "";
  for (const [skey, label] of [["staged", "策略 A：先锁强拍再配填充"],
                               ["joint", "策略 B：约束搜索同时决定变奏与收束"]]) {
    const o = resp.strategies[skey];
    if (!o) continue;
    const sec = document.createElement("div");
    sec.className = "var-strategy" +
      (resp.comparison.winner === skey ? " winner" : "");
    sec.innerHTML = `<h3>${label} — 枚举 ${o.summary.variants}，接受 ${o.summary.accepted}，舍弃 ${o.summary.rejected}</h3>`;
    o.accepted.forEach((cand, i) => sec.appendChild(variationCandidate(cand, true, i)));
    if (o.rejected.length) {
      const det = document.createElement("details");
      det.innerHTML = `<summary>被舍弃候选（${o.rejected.length}，含原因，可试听核对）</summary>`;
      o.rejected.forEach((cand, i) => det.appendChild(variationCandidate(cand, false, i)));
      sec.appendChild(det);
    }
    box.appendChild(sec);
  }
}

function variationCandidate(cand, accepted, idx) {
  const wrap = document.createElement("div");
  wrap.className = "var-cand" + (accepted ? "" : " rejected");
  const d = cand.distance;
  const meta = [
    `距离 总${d.total}（+${d.added} 增 / -${d.removed} 删 / 移${d.displaced}·${d.displacement_steps}步 / 力${d.velocity_changes}）`,
    `末拍 ${cand.fill_end.hits.length}/${cand.fill_end.budget}`,
    `切分 ${cand.scores.syncopation} · 重复 ${cand.scores.repetition}`,
  ].join("　");
  const head = document.createElement("div");
  head.className = "meta";
  head.innerHTML = `<b>${accepted ? "接受" : "舍弃"} #${idx + 1}</b><span>${meta}</span>`;
  wrap.appendChild(head);

  if (!accepted) {
    const rs = document.createElement("div");
    rs.className = "reasons";
    rs.textContent = "舍弃原因：" + cand.reasons.join("；");
    wrap.appendChild(rs);
  }

  const landing = document.createElement("div");
  landing.className = "meta";
  landing.innerHTML = cand.landing.map(l =>
    `落点 ${l.voice}@${l.step}：${l.hit ? "<span class='ok'>命中</span>" : "<span class='denied'>缺失</span>"}`).join("　");
  if (cand.landing.length) wrap.appendChild(landing);

  const canvas = document.createElement("canvas");
  canvas.className = "lanes";
  wrap.appendChild(canvas);
  drawLanes(canvas, cand.events, {});

  const acts = document.createElement("div");
  acts.className = "var-actions";
  const play = document.createElement("button");
  play.textContent = "试听" + (accepted ? "接受候选" : "舍弃候选");
  play.className = accepted ? "primary" : "";
  play.onclick = () => playEvents(cand.events, canvas, null, cand);
  const protoBtn = document.createElement("button");
  protoBtn.textContent = "试听原型对照";
  protoBtn.onclick = () => playEvents(state.variation.protoEvents, canvas, null, null);
  acts.appendChild(play); acts.appendChild(protoBtn);
  wrap.appendChild(acts);

  const det = document.createElement("details");
  det.className = "change-list";
  const denied = cand.changes.filter(c => !c.allowed).length;
  det.innerHTML = `<summary>逐项变化（${cand.changes.length} 项${denied ? `，<span class="denied">${denied} 项越界</span>` : "，全部在允许范围"}）</summary>` +
    "<ul>" + cand.changes.map(c =>
      `<li class="${c.allowed ? "ok" : "denied"}">${c.allowed ? "✓" : "✗"} ${c.voice}@${c.step} — ${c.detail}（${c.kind}）</li>`
    ).join("") + "</ul>";
  wrap.appendChild(det);

  if (cand.boundary.checks.length) {
    const bc = document.createElement("div");
    bc.className = "boundary-list";
    bc.innerHTML = "跨边界实际间隔核验：" + cand.boundary.checks.map(c =>
      `${c.scope} <span class="${c.ok ? "ok" : "fail"}">${c.gap_ms}ms / 要求 ${c.required_ms}ms ${c.ok ? "通过" : "不足"}</span>`
    ).join("；");
    wrap.appendChild(bc);
  }
  return wrap;
}

/* ---------------- 保存 / 载入 ---------------- */

async function saveVersion() {
  state.config.seed = parseInt(document.getElementById("seed").value) || 0;
  const payload = {
    seed: state.config.seed,
    config: state.config,
    chosen: state.chosenId,
    edited_events: state.edited,
  };
  try {
    const resp = await api("/api/save", payload);
    const blob = new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `percussion_${resp.id}.json`;
    a.click();
    setStatus(`已保存版本 ${resp.id}（含种子、规则与所选版本，可复算）`, "ok");
  } catch (e) {
    setStatus("保存失败：" + e.message, "error");
  }
}

async function loadVersion() {
  const id = document.getElementById("load-id").value.trim();
  if (!id) return;
  try {
    const resp = await api(`/api/load?id=${encodeURIComponent(id)}`);
    applyConfig(resp.config);
    document.getElementById("seed").value = resp.seed;
    setStatus(`已载入存档 ${id}，按种子复算中…`);
    await generate();
    if (resp.chosen !== null && resp.chosen !== undefined) {
      chooseForEdit(resp.chosen);
      if (resp.edited_events) {
        state.edited = resp.edited_events;
        drawEditor();
        validateEdited();
      }
    }
  } catch (e) {
    setStatus("载入失败：" + e.message, "error");
  }
}

function applyConfig(cfg) {
  state.config = cfg;
  document.getElementById("seed").value = cfg.seed;
  buildGlobalControls();
  buildVoices();
  buildRelations();
}

/* ---------------- 初始化 ---------------- */

async function init() {
  const data = await api("/api/default");
  state.presets = data.presets;
  state.catalog = data.voices_catalog;
  applyConfig(data.config);

  document.getElementById("generate").onclick = generate;
  document.getElementById("save").onclick = saveVersion;
  document.getElementById("load").onclick = loadVersion;
  document.querySelectorAll("[data-preset]").forEach(btn => {
    btn.onclick = () => {
      const p = state.presets[btn.dataset.preset];
      applyConfig(JSON.parse(JSON.stringify(p.config)));
      setStatus(`已载入预设：${p.label}`);
      document.getElementById("failure").classList.add("hidden");
      document.getElementById("candidates").innerHTML = "";
    };
  });

  const canvas = document.getElementById("editor-canvas");
  canvas.addEventListener("click", editorClick);
  canvas.addEventListener("contextmenu", ev => { ev.preventDefault(); deleteSelected(); });
  document.getElementById("vel-slider").oninput = ev => {
    if (state.selected >= 0) {
      state.edited[state.selected].velocity = parseFloat(ev.target.value);
      drawEditor();
      validateEdited();
    }
  };
  document.getElementById("validate").onclick = validateEdited;
  document.getElementById("audition-edit").onclick = auditionEdited;
  document.getElementById("run-variate").onclick = runVariation;
  document.addEventListener("keydown", ev => {
    if (ev.key === "Delete" || ev.key === "Backspace") {
      if (document.activeElement.tagName !== "INPUT") deleteSelected();
    }
  });
  setStatus("就绪。调整禁则后点击「生成节奏」。");
}

init();
