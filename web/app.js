"use strict";

// ───────────────────────── Utilidades ─────────────────────────
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (typeof v === "boolean") e[k] = v;
    else e.setAttribute(k, v);
  }
  return addKids(e, kids);
}

function addKids(e, kids) {
  for (const kid of kids.flat(3)) if (kid != null && kid !== false && kid !== "") e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}
// Como replaceChildren, pero acepta listas anidadas, nulos y textos.
const fill = (e, ...kids) => { e.replaceChildren(); return addKids(e, kids); };

async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(m);
  }
  return r.json();
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };
const pct = x => Math.round((x || 0) * 100) + "%";
const when = ts => new Date(ts * 1000).toLocaleString("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const short = (s, n = 90) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// Markdown mínimo y seguro: primero se escapa todo el HTML.
const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const inline = s => s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  .replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
function md(text) {
  const out = []; let list = null, para = [], code = null;
  const flushP = () => { if (para.length) { out.push("<p>" + inline(para.join("<br>")) + "</p>"); para = []; } };
  const flushL = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of esc(text).split("\n")) {
    const l = raw.trimEnd(); let m;
    if (code !== null) { if (l.trim().startsWith("```")) { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = null; } else code.push(raw); continue; }
    if (l.trim().startsWith("```")) { flushP(); flushL(); code = []; continue; }
    if (!l.trim()) { flushP(); flushL(); continue; }
    if ((m = l.match(/^(#{1,4})\s+(.*)/))) { flushP(); flushL(); const n = Math.min(m[1].length + 2, 6); out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if ((m = l.match(/^\s*[-*•]\s+(.*)/))) { flushP(); if (list !== "ul") { flushL(); out.push("<ul>"); list = "ul"; } out.push("<li>" + inline(m[1]) + "</li>"); continue; }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)/))) { flushP(); if (list !== "ol") { flushL(); out.push("<ol>"); list = "ol"; } out.push("<li>" + inline(m[1]) + "</li>"); continue; }
    if ((m = l.match(/^&gt;\s?(.*)/))) { flushP(); flushL(); out.push("<blockquote>" + inline(m[1]) + "</blockquote>"); continue; }
    flushL(); para.push(l);
  }
  if (code !== null) out.push("<pre><code>" + code.join("\n") + "</code></pre>");
  flushP(); flushL();
  return out.join("");
}

// ───────────────────────── Estado global ─────────────────────────
const S = { st: null, view: "discusion", module: null };
const view = name => $(`section[data-view="${name}"]`);
const provName = id => (S.st.providers.find(p => p.id === id) || {}).name || id;

async function refresh() {
  S.st = await api("/state");
  const e = S.st.embedder, real = S.st.providers.filter(p => p.ready && !p.demo);
  const name = (e.name || "").replace("fastembed:", "").split("/").pop();
  fill($("#foot"), 
    h("div", {}, h("span", { class: "dot " + (e.state === "listo" ? "ok" : e.state === "error" ? "bad" : "live") }),
      e.state === "listo" ? ["embeddings ", h("b", {}, name)] : e.state === "error" ? "error en embeddings" : "cargando modelo local…"),
    h("div", {}, h("span", { class: "dot " + (real.length ? "ok" : "warn") }), real.length ? [h("b", {}, real.length), " proveedor" + (real.length > 1 ? "es" : "") + " LLM"] : "modo demo, sin keys"),
    h("div", {}, h("b", {}, S.st.stats.cards), " tarjetas · ", h("b", {}, S.st.stats.docs), " notas"),
  );
  if (e.state === "cargando") setTimeout(guard(refresh), 3000);
  return S.st;
}

function go(name) {
  S.view = name;
  $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.go === name));
  $$("main > section").forEach(s => (s.hidden = s.dataset.view !== name));
  guard(SHOW[name])();
}

// ───────────────────────── Discusión ─────────────────────────
const NODES = [
  { id: "recuperar", label: "Memoria", x: 10, y: 14 },
  { id: "proponer", label: "Propuestas", x: 180, y: 14 },
  { id: "evaluar", label: "Evaluación", x: 350, y: 14 },
  { id: "criticar", label: "Críticas", x: 350, y: 104 },
  { id: "sintetizar", label: "Síntesis", x: 540, y: 14 },
  { id: "destilar", label: "Destilado", x: 710, y: 14 },
  { id: "guardar", label: "Guardado", x: 880, y: 14 },
];
const EDGES = [
  ["recuperar", "proponer", "M140 37H178"], ["proponer", "evaluar", "M310 37H348"],
  ["evaluar", "criticar", "M385 60V102"], ["criticar", "evaluar", "M445 104V62"],
  ["evaluar", "sintetizar", "M480 37H538"], ["sintetizar", "destilar", "M670 37H708"], ["destilar", "guardar", "M840 37H878"],
];
const IDLE = { recuperar: "búsqueda híbrida", proponer: "en paralelo", evaluar: "consenso", criticar: "rondas", sintetizar: "moderador", destilar: "tarjetas", guardar: "memoria" };

function svg(tag, attrs, ...kids) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  for (const kid of kids) e.append(kid.nodeType ? kid : document.createTextNode(kid));
  return e;
}
function pipeline() {
  const root = svg("svg", { class: "pipe", viewBox: "0 0 1020 160", role: "img", "aria-label": "Pasos de la discusión" });
  root.append(svg("defs", {}, svg("marker", { id: "arr", viewBox: "0 0 8 8", refX: "7", refY: "4", markerWidth: "7", markerHeight: "7", orient: "auto" }, svg("path", { d: "M0 0 8 4 0 8z" }))));
  for (const [a, b, d] of EDGES) root.append(svg("path", { class: "edge", d, "data-edge": `${a}-${b}`, "marker-end": "url(#arr)" }));
  for (const n of NODES) {
    root.append(svg("g", { class: "node", "data-node": n.id },
      svg("rect", { x: n.x, y: n.y, width: 130, height: 46, rx: 10 }),
      svg("text", { class: "n-label", x: n.x + 65, y: n.y + 20, "text-anchor": "middle" }, n.label),
      svg("text", { class: "n-sub", x: n.x + 65, y: n.y + 36, "text-anchor": "middle" }, IDLE[n.id])));
  }
  return h("div", { class: "pipe-wrap" }, root);
}
const node = (id, state, sub) => {
  const g = $(`[data-node="${id}"]`); if (!g) return;
  if (state) g.setAttribute("class", "node " + state);
  if (sub != null) $(".n-sub", g).textContent = sub;
};
const edge = (a, b, state) => { const e = $(`[data-edge="${a}-${b}"]`); if (e) e.setAttribute("class", "edge " + state); };

let R = null; // discusión abierta
let ES = null;

function buildDiscusion() {
  const problem = h("textarea", { id: "problem", placeholder: "¿Qué problema querés poner en discusión?", rows: 3 });
  problem.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRun(); });
  fill(view("discusion"), 
    h("div", { class: "head" }, h("h1", {}, "Discusión"), h("p", {}, "Varias LLM debaten tu problema; lo que acuerdan queda como conocimiento consultable.")),
    h("div", { id: "demo-notice" }),
    h("div", { class: "panel composer" }, problem,
      h("div", { class: "row" },
        h("div", { class: "row", id: "chips" }), h("div", { class: "spacer" }),
        h("label", { class: "row muted" }, "Rondas", h("select", { id: "rounds" }, [1, 2, 3, 4].map(n => h("option", { value: n, selected: n === 2 }, n)))),
        h("label", { class: "row muted" }, "Guardar en", h("select", { id: "auto-module" })),
        h("button", { class: "btn danger", id: "stop", hidden: true, onclick: guard(() => api(`/discussions/${R.id}/stop`, { method: "POST" })) }, "Detener"),
        h("button", { class: "btn primary", id: "start", onclick: () => startRun() }, "Iniciar discusión"))),
    h("div", { id: "stage", hidden: true },
      h("div", { class: "run-head" }, h("h2", { id: "run-title" }), h("span", { class: "tag", id: "run-status" }),
        h("button", { class: "btn small", onclick: () => closeRun() }, "← Historial")),
      pipeline(),
      h("div", { class: "live" },
        h("div", { class: "lanes", id: "lanes" }),
        h("div", { class: "side" },
          h("div", { class: "panel", id: "consensus" }),
          h("div", { class: "panel" }, h("div", { class: "label" }, "Memoria recuperada"), h("div", { id: "mem", style: "margin-top:8px" })),
          h("div", { class: "panel" }, h("div", { class: "label" }, "Actividad"), h("div", { class: "log", id: "log", style: "margin-top:8px" })))),
      h("div", { class: "result" },
        h("div", { class: "panel" }, h("div", { class: "row" }, h("h2", {}, "Síntesis"), h("span", { class: "mono muted", id: "synth-meta" })), h("div", { id: "synth", style: "margin-top:10px" })),
        h("div", { class: "panel" }, h("h2", {}, "Conocimiento destilado"), h("div", { id: "cards", style: "margin-top:10px" })))),
    h("div", { id: "history" }),
  );
}

async function showDiscusion() {
  const st = await refresh();
  fill($("#demo-notice"), st.demo ? h("div", { class: "notice" },
    h("span", {}, "Modo demo: todavía no hay ninguna API key cargada, así que las respuestas de los agentes son simuladas."),
    h("button", { class: "btn small", onclick: () => go("agentes") }, "Cargar una key gratuita")) : "");
  const picked = new Set($$("#chips .chip.on").map(c => +c.dataset.id));
  const first = !$("#chips").children.length;
  fill($("#chips"), st.agents.filter(a => !a.is_moderator).map(a =>
    h("button", { class: "chip" + ((first ? a.enabled : picked.has(a.id)) ? " on" : ""), "data-id": a.id, style: `--c:${a.color}`, title: a.role,
      onclick: e => e.currentTarget.classList.toggle("on") }, h("i"), a.name)));
  const sel = $("#auto-module"), cur = sel.value;
  fill(sel, h("option", { value: "" }, "revisar antes de guardar"), st.modules.map(m => h("option", { value: m.id }, m.name)));
  sel.value = cur;
  if (!R) loadHistory();
}

async function loadHistory() {
  const list = await api("/discussions");
  $("#history").hidden = false;
  fill($("#history"), 
    h("div", { class: "label", style: "margin:22px 0 10px" }, "Discusiones anteriores"),
    list.length ? h("div", { class: "stack" }, list.map(d => h("button", { class: "list-item", onclick: () => openRun(d.id) },
      h("span", { class: "dot " + (d.status === "terminada" ? "ok" : d.status === "en curso" ? "live" : "bad") }),
      h("span", { class: "t" }, d.problem), d.consensus != null ? h("span", { class: "tag" }, "consenso " + pct(d.consensus)) : "",
      h("span", { class: "tag" }, d.status), h("span", { class: "mono muted" }, when(d.created_at)))))
      : h("div", { class: "empty" }, "Todavía no hay discusiones. Escribí un problema arriba y poné a los agentes a debatir."));
}

const startRun = guard(async () => {
  const problem = $("#problem").value.trim();
  if (!problem) return toast("Escribí el problema a discutir", true);
  const agent_ids = $$("#chips .chip.on").map(c => +c.dataset.id);
  if (!agent_ids.length) return toast("Elegí al menos un agente", true);
  const { id } = await api("/discussions", { body: { problem, agent_ids, max_rounds: +$("#rounds").value, module_id: +$("#auto-module").value || null } });
  $("#problem").value = "";
  openRun(id);
});

function closeRun() {
  if (ES) { ES.close(); ES = null; }
  R = null;
  $("#stage").hidden = true; $("#stop").hidden = true; $("#start").disabled = false;
  loadHistory();
}

function openRun(id) {
  if (ES) ES.close();
  R = { id, t0: 0, lanes: new Map(), msgs: new Map(), evals: [], cards: [], live: false };
  $("#stage").hidden = false; $("#history").hidden = true;
  $("#run-title").textContent = ""; $("#run-status").textContent = "cargando"; $("#run-status").className = "tag";
  for (const n of NODES) node(n.id, "idle", IDLE[n.id]);
  for (const [a, b] of EDGES) edge(a, b, "");
  for (const sel of ["#lanes", "#mem", "#log", "#synth", "#cards", "#consensus"]) fill($(sel));
  $("#synth-meta").textContent = "";
  $("#consensus").append(h("div", { class: "label" }, "Consenso"), h("p", { class: "muted", style: "margin-top:8px" }, "Se mide al terminar cada ronda."));
  ES = new EventSource(`/api/discussions/${id}/events`);
  ES.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === "end") { ES.close(); ES = null; return endRun(); }
    try { (ON[ev.type] || (() => {}))(ev); } catch (err) { console.error(ev.type, err); }
  };
  ES.onerror = () => { if (ES) { ES.close(); ES = null; toast("Se cortó la conexión con el servidor", true); } };
}

function log(ev, text, cls) {
  const dt = R.t0 ? Math.max(0, ev.t - R.t0) : 0;
  const box = $("#log");
  box.append(h("div", { class: cls || "" }, h("time", {}, "+" + dt.toFixed(1) + "s"), h("span", {}, text)));
  box.scrollTop = box.scrollHeight;
}
function status(text, cls) { const s = $("#run-status"); s.textContent = text; s.className = "tag " + (cls || ""); }

function lane(a) {
  const st = h("span", { class: "tag" }, "esperando"), model = h("div", { class: "lane-model" }, provName(a.pid));
  const body = h("div", { class: "lane-body" });
  const el = h("div", { class: "lane", style: `--c:${a.color}` },
    h("div", { class: "lane-head", title: a.role }, h("div", { class: "row" }, h("strong", {}, a.name), h("span", { class: "spacer" }), st), model), body);
  R.lanes.set(a.id, { el, st, model, body });
  return el;
}

function message(ev) {
  let m = R.msgs.get(ev.mid);
  if (m) return m;
  const synth = ev.stage === "sintetizar";
  const body = h("div", { class: "md streaming" }), meta = h("span", { class: "msg-meta" });
  const title = ev.stage === "proponer" ? "Propuesta" : `Crítica · ronda ${ev.round}`;
  const wrap = synth ? body : h("div", { class: "msg" }, h("div", { class: "msg-head" }, h("span", { class: "msg-title" }, title), meta), body);
  const L = R.lanes.get(ev.agent_id);
  (synth ? $("#synth") : L.body).append(wrap);
  m = { body, meta, wrap, text: "", raf: 0, lane: synth ? null : L, scroller: synth ? null : L.body };
  R.msgs.set(ev.mid, m);
  return m;
}
function paint(m) {
  if (m.raf) return;
  m.raf = requestAnimationFrame(() => {
    m.raf = 0;
    const s = m.scroller, stick = s && s.scrollHeight - s.scrollTop - s.clientHeight < 60;
    m.body.innerHTML = md(m.text);
    if (stick) s.scrollTop = s.scrollHeight;
  });
}

const ON = {
  run_started(ev) {
    R.t0 = ev.t; R.live = true; R.agents = ev.agents; R.maxRounds = ev.max_rounds;
    $("#run-title").textContent = ev.problem;
    status(ev.demo ? "modo demo" : "en curso", "warn");
    fill($("#lanes"), ev.agents.map(lane));
    $("#stop").hidden = false; $("#start").disabled = true;
    log(ev, `Arranca la discusión: ${ev.agents.length} agentes, hasta ${ev.max_rounds} ronda${ev.max_rounds > 1 ? "s" : ""}`, "hi");
  },
  step_started(ev) {
    const s = ev.step;
    node(s, "active", s === "criticar" ? `ronda ${ev.round}` : s === "evaluar" ? `ronda ${ev.round}` : s === "proponer" ? `${R.agents.length} agentes` : "trabajando…");
    const from = { proponer: "recuperar", sintetizar: "evaluar", destilar: "sintetizar", guardar: "destilar" }[s];
    if (from) edge(from, s, "active");
    if (s === "criticar") edge("evaluar", "criticar", "active");
    if (s === "evaluar") edge(ev.round === 1 ? "proponer" : "criticar", "evaluar", "active");
    log(ev, { recuperar: "Buscando en la memoria y los módulos", proponer: "Los agentes escriben su propuesta en paralelo", evaluar: `El moderador evalúa el consenso de la ronda ${ev.round}`,
      criticar: `Ronda ${ev.round}: cada agente critica a los demás`, sintetizar: "El moderador escribe la síntesis", destilar: "Extrayendo tarjetas de conocimiento", guardar: "Guardando en la memoria" }[s]);
  },
  step_finished(ev) {
    const s = ev.step;
    for (const [a, b] of EDGES) if (b === s && $(`[data-edge="${a}-${b}"]`).classList.contains("active")) edge(a, b, "done");
    if (s === "recuperar") {
      node(s, "done", ev.items.length ? `${ev.items.length} recuerdos` : "sin recuerdos");
      fill($("#mem"), ev.items.length ? ev.items.map(i => h("div", { class: "mem" },
        h("b", {}, i.title), h("p", {}, i.text), h("div", { class: "row", style: "margin-top:4px" }, h("span", { class: "tag" }, i.kind), h("span", { class: "tag" }, "relevancia " + pct(i.confidence)))))
        : h("p", { class: "muted" }, "Nada relacionado todavía. Lo que salga de esta discusión queda guardado para la próxima."));
      log(ev, ev.items.length ? `Recuperó ${ev.items.length} recuerdos relevantes` : "No encontró recuerdos relacionados");
    } else if (s === "proponer" || s === "criticar") {
      node(s, "done", s === "proponer" ? "listas" : `ronda ${ev.round} lista`);
    } else if (s === "evaluar") {
      node(s, "done", "consenso " + pct(ev.consensus));
    } else if (s === "sintetizar") {
      node(s, "done", "lista");
    } else if (s === "destilar") {
      node(s, ev.cards.length ? "done" : "error", `${ev.cards.length} tarjetas`);
      R.cards = ev.cards; renderCards();
      log(ev, ev.cards.length ? `Destiló ${ev.cards.length} tarjetas de conocimiento` : ev.warning, ev.cards.length ? "" : "err");
    } else if (s === "guardar") {
      node(s, "done", ev.saved ? `${ev.saved} en módulo` : "en memoria");
      log(ev, "Síntesis guardada en la memoria" + (ev.saved ? ` y ${ev.saved} tarjetas en el módulo` : ""));
    }
  },
  message_start(ev) {
    const m = message(ev);
    m.text = ""; m.body.className = "md streaming"; m.body.innerHTML = ""; m.meta.textContent = "";
    $$(".msg-err", m.wrap).forEach(e => e.remove());
    const where = `${provName(ev.provider)} · ${ev.model}`;
    if (m.lane) {
      m.lane.st.textContent = "escribiendo…"; m.lane.st.className = "tag ok"; m.lane.model.textContent = where; m.lane.el.classList.add("speaking");
      m.lane.body.scrollTop = m.lane.body.scrollHeight;  // que se vea la intervención nueva
    }
    else $("#synth-meta").textContent = `${ev.agent} · ${where}`;
    if (ev.backup) log(ev, `${ev.agent} sigue con un modelo de respaldo (${where})`);
  },
  message_delta(ev) { const m = R.msgs.get(ev.mid); if (m) { m.text += ev.delta; paint(m); } },
  message_reset(ev) { const m = R.msgs.get(ev.mid); if (m) { m.text = ""; paint(m); } },
  message_end(ev) {
    const m = R.msgs.get(ev.mid); if (!m) return;
    m.text = ev.text; m.body.className = "md"; m.body.innerHTML = md(ev.text);
    const secs = (ev.ms / 1000).toFixed(1) + " s";
    m.meta.textContent = secs;
    if (m.lane) { m.lane.st.textContent = "listo"; m.lane.st.className = "tag"; m.lane.el.classList.remove("speaking"); m.lane.model.textContent = `${provName(ev.provider)} · ${ev.model}`; }
    const who = m.lane ? R.agents.find(a => R.lanes.get(a.id) === m.lane).name : "El moderador";
    log(ev, `${who} terminó en ${secs}`);
  },
  message_error(ev) {
    log(ev, `${ev.agent}: ${ev.error}` + (ev.retrying ? " → pruebo con un respaldo" : ""), "err");
    const L = R.lanes.get(ev.agent_id);
    if (!ev.visible || !L) return;
    L.el.classList.remove("speaking");
    if (!ev.retrying) { L.st.textContent = "sin respuesta"; L.st.className = "tag bad"; }
    const m = R.msgs.get(ev.mid);
    if (m) { m.body.className = "md"; m.wrap.append(h("div", { class: "msg-err" }, ev.error)); }
  },
  evaluation(ev) {
    R.evals.push(ev);
    edge("evaluar", ev.next, "active");
    const n = ev.names.length, ini = ev.names.map(x => x.slice(0, 2));
    const cell = v => h("span", { style: `background:color-mix(in srgb, var(--accent) ${Math.round(Math.max(0, v) * 70)}%, transparent)`, title: pct(v) }, Math.round(v * 100));
    fill($("#consensus"), 
      h("div", { class: "label" }, "Consenso"),
      h("div", { class: "gauge-num", style: "margin-top:8px" }, pct(ev.consensus)),
      h("div", { class: "gauge-sub" }, `ronda ${ev.round} · ${ev.by === "juez" ? "según el moderador" : "estimado con embeddings"}`),
      h("div", { class: "bar" }, h("i", { style: `width:${pct(ev.consensus)}` })),
      R.evals.length > 1 ? h("div", { class: "rounds" }, R.evals.map(e => h("span", { class: "tag" }, `R${e.round} ${pct(e.consensus)}`))) : "",
      ev.agreements.length ? [h("div", { class: "label", style: "margin-top:14px" }, "Acuerdos"), h("ul", { class: "points yes" }, ev.agreements.map(x => h("li", {}, x)))] : "",
      ev.disagreements.length ? [h("div", { class: "label", style: "margin-top:14px" }, "Desacuerdos"), h("ul", { class: "points no" }, ev.disagreements.map(x => h("li", {}, x)))] : "",
      n > 1 ? [h("div", { class: "label", style: "margin-top:14px" }, "Similitud entre posturas"),
        h("div", { class: "matrix", style: `grid-template-columns:repeat(${n + 1}, 1fr)` },
          h("span"), ini.map(i => h("span", { class: "hd" }, i)),
          ev.matrix.map((row, r) => [h("span", { class: "hd" }, ini[r]), row.map(cell)])),
        h("p", { class: "mono muted", style: "margin-top:6px" }, "embeddings locales, sin LLM")] : "");
    log(ev, `Consenso ${pct(ev.consensus)} → ` + (ev.next === "criticar" ? "hace falta otra ronda" : "pasa a la síntesis"), "hi");
  },
  run_finished(ev) {
    R.live = false;
    status("terminada", "ok");
    log(ev, `Discusión terminada en ${ev.seconds} s`, "hi");
  },
  run_error(ev) {
    R.live = false;
    status(ev.cancelled ? "detenida" : "error", "bad");
    $$(".node.active").forEach(g => g.setAttribute("class", "node error"));
    $$(".md.streaming").forEach(e => (e.className = "md"));
    log(ev, ev.error, "err");
    if (!ev.cancelled) toast(ev.error, true);
  },
};

async function endRun() {
  $("#stop").hidden = true; $("#start").disabled = false;
  if (!R) return;
  if ($("#run-status").textContent === "cargando") status("interrumpida", "bad");
  const d = await api(`/discussions/${R.id}`);
  const done = new Map(d.proposals.map(p => [p.id, p.card_id]));
  R.cards.forEach(c => (c.saved = !!done.get(c.id)));
  renderCards();
  refresh();
}

function renderCards() {
  const box = $("#cards");
  if (!R.cards.length) return fill(box, h("p", { class: "muted" }, "Cuando termine la síntesis, acá aparecen las tarjetas que se pueden guardar en un módulo."));
  const pending = R.cards.filter(c => !c.saved);
  const sel = h("select", {}, S.st.modules.map(m => h("option", { value: m.id }, m.name)), h("option", { value: "new" }, "＋ módulo nuevo…"));
  const name = h("input", { type: "text", placeholder: "Nombre del módulo", style: "max-width:200px", hidden: !!S.st.modules.length });
  sel.addEventListener("change", () => (name.hidden = sel.value !== "new"));
  fill(box, 
    R.cards.map(c => h("div", { class: "kcard" + (c.saved ? " saved" : "") },
      h("input", { type: "checkbox", checked: !c.saved, disabled: !!c.saved, "data-id": c.id }),
      h("div", { class: "q" }, c.question), h("div", { class: "a" }, c.answer),
      h("div", { class: "meta" }, h("span", { class: "tag" }, c.kind), c.tags.map(t => h("span", { class: "tag" }, "#" + t)), c.saved ? h("span", { class: "tag ok" }, "guardada") : ""))),
    pending.length ? h("div", { class: "row", style: "margin-top:12px" }, h("span", { class: "muted" }, "Guardar en"), sel, name,
      h("button", { class: "btn primary", onclick: guard(async () => {
        const ids = $$("#cards input:checked:not(:disabled)").map(i => +i.dataset.id);
        if (!ids.length) return toast("Marcá al menos una tarjeta", true);
        const body = sel.value === "new" ? { new_module: name.value, ids } : { module_id: +sel.value, ids };
        const r = await api(`/discussions/${R.id}/accept`, { body });
        toast(`${r.saved} tarjetas guardadas`);
        R.cards.forEach(c => { if (ids.includes(c.id)) c.saved = true; });
        await refresh(); renderCards();
      }) }, "Guardar tarjetas")) : h("p", { class: "muted", style: "margin-top:10px" }, "Todas las tarjetas ya están en un módulo. Probalas en Consultar."));
}

// ───────────────────────── Consultar (sin LLM) ─────────────────────────
function buildConsultar() {
  const q = h("input", { type: "search", id: "ask-q", placeholder: "Preguntale a tus módulos…", autocomplete: "off" });
  q.addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
  fill(view("consultar"), h("div", { class: "ask" },
    h("div", { class: "head" }, h("h1", {}, "Consultar"), h("p", {}, "Responde desde tus módulos con búsqueda local. No llama a ninguna LLM.")),
    h("div", { class: "ask-box" }, q, h("button", { class: "btn primary", onclick: () => ask() }, "Preguntar")),
    h("div", { class: "row", id: "ask-filters", style: "margin-top:12px" }),
    h("div", { id: "ask-out" })));
}
async function showConsultar() {
  const st = await refresh(), on = new Set($$("#ask-filters .chip.on").map(c => +c.dataset.id));
  const ollama = st.providers.find(p => p.id === "ollama");
  fill($("#ask-filters"), 
    st.modules.length ? h("span", { class: "muted" }, "Buscar en") : "",
    st.modules.map(m => h("button", { class: "chip" + (on.has(m.id) ? " on" : ""), "data-id": m.id, onclick: e => e.currentTarget.classList.toggle("on") }, h("i"), `${m.name} (${m.cards})`)),
    st.modules.length ? h("span", { class: "muted mono" }, "sin marcar = todos") : "",
    ollama && ollama.ready ? h("label", { class: "switch" }, h("input", { type: "checkbox", id: "ask-local" }), "redactar con el modelo local (Ollama)") : "");
  if (!st.modules.length) fill($("#ask-out"), h("div", { class: "empty", style: "margin-top:16px" }, "Todavía no hay módulos. Se crean guardando las tarjetas de una discusión, o a mano en Módulos."));
  $("#ask-q").focus();
}
const ask = guard(async () => {
  const q = $("#ask-q").value.trim(); if (!q) return;
  const body = { q, module_ids: $$("#ask-filters .chip.on").map(c => +c.dataset.id), local_model: !!($("#ask-local") && $("#ask-local").checked) };
  const r = await api("/ask", { body });
  const rel = c => h("div", { class: "kcard" }, h("span"), h("div", { class: "q" }, c.question), h("div", { class: "a" }, c.answer),
    h("div", { class: "meta" }, h("span", { class: "tag" }, c.module), h("span", { class: "tag" }, "cercanía " + pct(c.confidence))));
  const out = [];
  if (r.answered) {
    const c = r.card;
    out.push(h("div", { class: "answer " + r.level },
      r.composed ? [h("div", { class: "a md", html: md(r.composed.text) }), h("p", { class: "mono muted", style: "margin:10px 0" }, `redactado por ${r.composed.model} en tu máquina, a partir de:`)] : "",
      h("div", { class: "q" }, c.question), h("div", { class: r.composed ? "muted" : "a" }, c.answer),
      h("div", { class: "foot" },
        h("span", { class: "tag " + (r.level === "alta" ? "ok" : "warn") }, r.level === "alta" ? "confianza alta " + pct(r.confidence) : "coincidencia parcial " + pct(r.confidence) + ", conviene verificar"),
        h("span", { class: "tag" }, "módulo: " + c.module), c.source ? h("span", { class: "tag" }, c.source) : "",
        h("span", { class: "tag" }, `${r.ms} ms · sin LLM`))));
    if (r.composed_error) out.push(h("p", { class: "muted", style: "margin-top:8px" }, r.composed_error));
  } else {
    out.push(h("div", { class: "notice", style: "margin-top:16px" }, h("span", {}, "No encontré esto en tus módulos."),
      h("button", { class: "btn small primary", onclick: () => { go("discusion"); $("#problem").value = q; $("#problem").focus(); } }, "Ponerlo en discusión")));
  }
  if (r.related.length) out.push(h("div", { class: "label", style: "margin:20px 0 10px" }, r.answered ? "Relacionado" : "Lo más cercano que hay"), r.related.map(rel));
  fill($("#ask-out"), ...out.flat());
});

// ───────────────────────── Módulos ─────────────────────────
const KINDS = ["hecho", "procedimiento", "decision", "definicion", "advertencia"];

function buildModulos() {
  fill(view("modulos"), 
    h("div", { class: "head" }, h("h1", {}, "Módulos"), h("p", {}, "Paquetes de conocimiento por tema. Se consultan sin LLM y se pueden exportar.")),
    h("div", { class: "grid2" }, h("div", { id: "mod-list", class: "stack" }), h("div", { id: "mod-detail" })));
}
async function showModulos() {
  const st = await refresh();
  if (!st.modules.find(m => m.id === S.module)) S.module = st.modules[0] ? st.modules[0].id : null;
  const name = h("input", { type: "text", placeholder: "Nuevo módulo…" });
  const file = h("input", { type: "file", accept: ".json", hidden: true, onchange: guard(async e => {
    const f = e.target.files[0]; if (!f) return;
    const r = await api("/modules/import", { body: JSON.parse(await f.text()) });
    toast(`Módulo «${r.name}» importado con ${r.cards} tarjetas`); S.module = r.id; showModulos();
  }) });
  const create = guard(async () => { if (!name.value.trim()) return; const r = await api("/modules", { body: { name: name.value } }); S.module = r.id; showModulos(); });
  name.addEventListener("keydown", e => { if (e.key === "Enter") create(); });
  fill($("#mod-list"), 
    st.modules.map(m => h("button", { class: "list-item" + (m.id === S.module ? " on" : ""), onclick: () => { S.module = m.id; showModulos(); } },
      h("span", { class: "t" }, m.name), h("span", { class: "tag" }, m.cards + " tarjetas"))),
    h("div", { class: "row", style: "flex-wrap:nowrap" }, name, h("button", { class: "btn", onclick: create }, "Crear")),
    file, h("button", { class: "btn", onclick: () => file.click() }, "Importar módulo (.json)"));
  const box = $("#mod-detail");
  if (!S.module) return fill(box, h("div", { class: "empty" }, "Creá un módulo, o guardá las tarjetas de una discusión para que se cree solo."));
  const m = st.modules.find(x => x.id === S.module), cards = await api(`/modules/${m.id}/cards`);
  fill(box, h("div", { class: "panel" },
    h("div", { class: "row" }, h("h2", {}, m.name), h("span", { class: "spacer" }),
      h("button", { class: "btn small primary", onclick: () => cardDialog(m.id) }, "＋ Tarjeta"),
      h("a", { class: "btn small", href: `/api/modules/${m.id}/export.html`, title: "Una página que se abre con doble clic y busca sin servidor ni IA" }, "Exportar página autónoma"),
      h("a", { class: "btn small", href: `/api/modules/${m.id}/export.json` }, "Exportar .json"),
      h("button", { class: "btn small danger", onclick: guard(async () => { if (!confirm(`¿Borrar el módulo «${m.name}» y sus ${cards.length} tarjetas?`)) return; await api(`/modules/${m.id}`, { method: "DELETE" }); S.module = null; showModulos(); }) }, "Borrar")),
    h("div", { style: "margin-top:14px" }, cards.length ? cards.map(c => h("div", { class: "kcard" }, h("span"),
      h("div", { class: "q" }, c.question), h("div", { class: "a" }, c.answer),
      h("div", { class: "meta" }, h("span", { class: "tag" }, c.kind), c.tags.map(t => h("span", { class: "tag" }, "#" + t)), c.source ? h("span", { class: "tag" }, c.source) : "",
        c.variants.length ? h("span", { class: "tag", title: c.variants.join("\n") }, `${c.variants.length} variantes`) : "", h("span", { class: "spacer" }),
        h("button", { class: "btn small", onclick: () => cardDialog(m.id, c) }, "Editar"),
        h("button", { class: "btn small danger", onclick: guard(async () => { await api(`/cards/${c.id}`, { method: "DELETE" }); showModulos(); }) }, "Borrar"))))
      : h("div", { class: "empty" }, "Este módulo todavía no tiene tarjetas."))));
}
function cardDialog(moduleId, card) {
  const c = card || { question: "", answer: "", kind: "hecho", tags: [], variants: [] };
  const q = h("input", { type: "text", value: c.question }), a = h("textarea", { rows: 5 }, c.answer);
  const kind = h("select", {}, KINDS.map(k => h("option", { value: k, selected: k === c.kind }, k)));
  const variants = h("textarea", { rows: 3, placeholder: "Una por línea" }, c.variants.join("\n")), tags = h("input", { type: "text", value: c.tags.join(", ") });
  const dlg = $("#dialog");
  fill(dlg, h("div", { class: "stack" }, h("h2", {}, card ? "Editar tarjeta" : "Nueva tarjeta"),
    h("label", { class: "field" }, "Pregunta", q), h("label", { class: "field" }, "Respuesta (tiene que entenderse sola)", a),
    h("label", { class: "field" }, "Otras formas de preguntarlo", variants),
    h("div", { class: "row" }, h("label", { class: "field" }, "Tipo", kind), h("label", { class: "field", style: "flex:1" }, "Etiquetas, separadas por coma", tags)),
    h("div", { class: "row" }, h("span", { class: "spacer" }), h("button", { class: "btn", onclick: () => dlg.close() }, "Cancelar"),
      h("button", { class: "btn primary", onclick: guard(async () => {
        const body = { question: q.value, answer: a.value, kind: kind.value, tags: tags.value.split(",").map(s => s.trim()).filter(Boolean), variants: variants.value.split("\n").map(s => s.trim()).filter(Boolean) };
        if (card) await api(`/cards/${card.id}`, { method: "PUT", body }); else await api(`/modules/${moduleId}/cards`, { body });
        dlg.close(); showModulos();
      }) }, "Guardar"))));
  dlg.showModal();
}

// ───────────────────────── Memoria ─────────────────────────
function buildMemoria() {
  const title = h("input", { type: "text", id: "doc-title", placeholder: "Título" }), text = h("textarea", { id: "doc-text", rows: 6, placeholder: "Pegá acá la información nueva…" });
  const file = h("input", { type: "file", accept: ".txt,.md,.markdown,.csv,.json,.log", hidden: true, onchange: async e => {
    const f = e.target.files[0]; if (!f) return; text.value = await f.text(); if (!title.value) title.value = f.name.replace(/\.[^.]+$/, ""); } });
  const sq = h("input", { type: "search", placeholder: "Probar una búsqueda en la memoria…" });
  sq.addEventListener("keydown", guard(async e => {
    if (e.key !== "Enter" || !sq.value.trim()) return;
    const r = await api("/search?q=" + encodeURIComponent(sq.value));
    fill($("#mem-hits"), r.length ? r.map(x => h("div", { class: "mem" }, h("b", {}, x.title), h("p", {}, x.text), h("span", { class: "tag" }, "relevancia " + pct(x.confidence)))) : h("p", { class: "muted" }, "Sin resultados."));
  }));
  fill(view("memoria"), 
    h("div", { class: "head" }, h("h1", {}, "Memoria"), h("p", {}, "El contexto general va en todas las discusiones. Las notas se recuperan solo cuando son relevantes.")),
    h("div", { class: "grid2", style: "grid-template-columns:minmax(0,1fr) minmax(0,1fr)" },
      h("div", {},
        h("div", { class: "panel" }, h("h2", {}, "Contexto general"), h("textarea", { id: "ctx", rows: 8, oninput: e => (e.target.dataset.dirty = "1"), placeholder: "Quién sos, en qué estás trabajando, restricciones, glosario… Todo lo que los agentes tienen que saber siempre." }),
          h("div", { class: "row", style: "margin-top:10px" }, h("span", { class: "spacer" }), h("button", { class: "btn primary", onclick: guard(async () => { await api("/context", { method: "PUT", body: { text: $("#ctx").value } }); delete $("#ctx").dataset.dirty; toast("Contexto guardado"); }) }, "Guardar contexto"))),
        h("div", { class: "panel" }, h("h2", {}, "Agregar información"), h("div", { class: "stack" }, title, text, file,
          h("div", { class: "row" }, h("button", { class: "btn", onclick: () => file.click() }, "Cargar archivo de texto"), h("span", { class: "spacer" }),
            h("button", { class: "btn primary", onclick: guard(async () => {
              const r = await api("/docs-list", { body: { title: title.value, text: text.value } });
              toast(`«${r.title}» guardado en ${r.chunks} fragmento${r.chunks > 1 ? "s" : ""}`); title.value = text.value = ""; showMemoria();
            }) }, "Guardar en memoria"))))),
      h("div", {},
        h("div", { class: "panel" }, h("h2", {}, "Buscar"), sq, h("div", { id: "mem-hits", style: "margin-top:10px" })),
        h("div", { class: "panel" }, h("h2", {}, "Lo que hay guardado"), h("div", { id: "doc-list", class: "stack" })))));
}
async function showMemoria() {
  await refresh();
  const [ctx, docs] = await Promise.all([api("/context"), api("/docs-list")]);
  if (!$("#ctx").dataset.dirty) $("#ctx").value = ctx.text;  // no pisar lo que se está escribiendo
  fill($("#doc-list"), docs.length ? docs.map(d => h("div", { class: "list-item", style: "cursor:default" },
    h("span", { class: "t" }, d.title), h("span", { class: "tag" }, d.kind), h("span", { class: "mono muted" }, when(d.created_at)),
    h("button", { class: "btn small danger", onclick: guard(async () => { await api(`/docs-list/${d.id}`, { method: "DELETE" }); showMemoria(); }) }, "Borrar")))
    : h("div", { class: "empty" }, "La memoria está vacía. Las síntesis de las discusiones se guardan acá solas."));
}

// ───────────────────────── Agentes y proveedores ─────────────────────────
function buildAgentes() {
  fill(view("agentes"), 
    h("div", { class: "head" }, h("h1", {}, "Agentes"), h("p", {}, "Cada agente tiene un rol y puede correr en un proveedor distinto. En «automático» se reparten entre los que tengas configurados.")),
    h("div", { class: "cards", id: "agents" }),
    h("div", { class: "head", style: "margin-top:30px" }, h("h1", {}, "Proveedores de LLM"), h("p", {}, "Las keys se guardan solo en tu máquina (data/keys.json).")),
    h("div", { class: "cards", id: "providers" }));
}

async function modelOptions(sel, pid, current) {
  fill(sel, h("option", { value: "auto" }, "automático"));
  if (pid !== "auto") {
    const r = await api(`/providers/${pid}/models`).catch(() => ({ models: [] }));
    r.models.forEach(m => sel.append(h("option", { value: m }, m)));
  }
  if (current && current !== "auto" && ![...sel.options].some(o => o.value === current)) sel.append(h("option", { value: current }, current));
  sel.value = current || "auto";
}

function agentCard(a, providers) {
  const isNew = !a.id;
  const name = h("input", { type: "text", value: a.name, placeholder: "Nombre" }), color = h("input", { type: "color", value: a.color });
  const role = h("textarea", { rows: 4, placeholder: "Rol: cómo piensa y qué prioriza este agente" }, a.role);
  const prov = h("select", {}, h("option", { value: "auto" }, "automático"), providers.map(p => h("option", { value: p.id }, p.name + (p.ready ? "" : " (sin configurar)"))));
  prov.value = a.provider;
  const model = h("select", { style: "min-width:0;width:100%" });
  modelOptions(model, a.provider, a.model);
  prov.addEventListener("change", () => modelOptions(model, prov.value, "auto"));
  const temp = h("input", { type: "range", min: 0, max: 1.2, step: 0.1, value: a.temperature, style: "flex:1;accent-color:var(--accent)" });
  const on = h("input", { type: "checkbox", checked: !!a.enabled });
  const card = h("div", { class: "panel agent", style: `--c:${a.color};margin:0` }, h("div", { class: "stack" },
    h("div", { class: "row", style: "flex-wrap:nowrap" }, color, name, a.is_moderator ? h("span", { class: "tag ok" }, "moderador") : ""),
    role,
    h("div", { class: "two" }, h("label", { class: "field" }, "Proveedor", prov), h("label", { class: "field" }, "Modelo", model)),
    h("label", { class: "field" }, "Temperatura (más alta = más variado)", h("div", { class: "row" }, temp)),
    h("div", { class: "row" },
      a.is_moderator ? h("span", { class: "muted", style: "font-size:12.5px" }, "Evalúa, sintetiza y destila") : h("label", { class: "switch" }, on, "participa por defecto"),
      h("span", { class: "spacer" }),
      !isNew && !a.is_moderator ? h("button", { class: "btn small danger", onclick: guard(async () => { if (!confirm(`¿Borrar al agente «${a.name}»?`)) return; await api(`/agents/${a.id}`, { method: "DELETE" }); showAgentes(); }) }, "Borrar") : "",
      h("button", { class: "btn small primary", onclick: guard(async () => {
        const body = { name: name.value, role: role.value, provider: prov.value, model: model.value, color: color.value, temperature: +temp.value, enabled: a.is_moderator ? true : on.checked };
        if (isNew) await api("/agents", { body }); else await api(`/agents/${a.id}`, { method: "PUT", body });
        toast("Agente guardado"); showAgentes();
      }) }, isNew ? "Crear agente" : "Guardar"))));
  color.addEventListener("input", () => card.style.setProperty("--c", color.value));
  return card;
}

function providerCard(p) {
  const res = h("div", { class: "res" });
  const test = h("button", { class: "btn small", onclick: guard(async () => {
    res.textContent = "probando…";
    const r = await api(`/providers/${p.id}/test`, { method: "POST" });
    res.textContent = r.ok ? `✓ respondió ${r.model} en ${r.ms} ms · ${r.models.length} modelos disponibles` : "✕ " + r.error;
    res.style.color = r.ok ? "var(--accent)" : "var(--bad)";
  }) }, "Probar");
  const state = p.demo ? ["siempre disponible", "ok"] : p.ready ? [p.local ? "detectado" : "key cargada " + p.key_hint, "ok"] : [p.local ? "no detectado" : "falta la key", "warn"];
  const kids = [h("div", { class: "row" }, h("h3", {}, p.name), h("span", { class: "spacer" }), h("span", { class: "tag " + state[1] }, state[0])), h("p", { class: "note" }, p.note)];
  if (p.demo) kids.push(h("div", { class: "row" }, test));
  else if (p.local) kids.push(h("div", { class: "row" }, h("a", { class: "btn small", href: p.signup, target: "_blank", rel: "noopener" }, "Descargar Ollama"), h("span", { class: "spacer" }), test));
  else {
    const key = h("input", { type: "password", placeholder: p.ready ? "Reemplazar la key…" : "Pegá tu API key", autocomplete: "off" });
    const save = guard(async () => { await api(`/providers/${p.id}/key`, { method: "PUT", body: { key: key.value.trim() } }); toast(key.value.trim() ? "Key guardada" : "Key borrada"); showAgentes(); });
    kids.push(h("div", { class: "row", style: "flex-wrap:nowrap" }, key, h("button", { class: "btn small primary", onclick: save }, "Guardar")),
      h("div", { class: "row", style: "margin-top:8px" }, h("a", { class: "btn small", href: p.signup, target: "_blank", rel: "noopener" }, "Conseguir key gratis"), h("span", { class: "spacer" }),
        p.ready ? h("button", { class: "btn small danger", onclick: () => { key.value = ""; save(); } }, "Quitar") : "", p.ready ? test : ""));
  }
  kids.push(res);
  return h("div", { class: "panel prov", style: "margin:0" }, kids);
}

async function showAgentes() {
  const st = await refresh();
  const blank = { name: "", role: "", provider: "auto", model: "auto", color: "#7aa2f7", temperature: 0.7, enabled: 1 };
  fill($("#agents"), st.agents.map(a => agentCard(a, st.providers)), agentCard(blank, st.providers));
  fill($("#providers"), st.providers.map(providerCard));
}

// ───────────────────────── Inicio ─────────────────────────
const SHOW = { discusion: showDiscusion, consultar: showConsultar, modulos: showModulos, memoria: showMemoria, agentes: showAgentes };

(async function init() {
  buildDiscusion(); buildConsultar(); buildModulos(); buildMemoria(); buildAgentes();
  $$("#nav button").forEach(b => b.addEventListener("click", () => go(b.dataset.go)));
  try {
    await showDiscusion();
    if (S.st.running.length) openRun(S.st.running[0]);
  } catch (e) { toast("No se pudo conectar con el servidor: " + e.message, true); }
})();
