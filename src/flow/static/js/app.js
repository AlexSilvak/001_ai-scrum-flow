/* AI Scrum Flow — dashboard. Sem framework; todo texto dinâmico passa por esc() (conteúdo vem de LLMs). */
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const apiToken = () => { try { return localStorage.getItem("flowApiToken") || ""; } catch { return ""; } };
let asking = false;
async function askToken() {  // 401: pede o FLOW_API_TOKEN uma vez e guarda no navegador
  if (asking) return false;
  asking = true;
  const t = window.prompt("A API exige token. Cole o valor de FLOW_API_TOKEN (do seu .env):");
  asking = false;
  if (!t) return false;
  try { localStorage.setItem("flowApiToken", t.trim()); } catch {}
  return true;
}
const api = async (path, opts = {}, retry = true) => {
  const r = await fetch("/api" + path, { headers: { "Content-Type": "application/json", ...(apiToken() ? { Authorization: "Bearer " + apiToken() } : {}) }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (r.status === 401 && retry && (await askToken())) return api(path, opts, false);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const toast = (msg, bad = false) => {
  const t = $("#toast"); t.textContent = msg; t.className = `fixed bottom-4 left-1/2 z-[60] -translate-x-1/2 rounded-lg border px-4 py-2 text-sm ${bad ? "border-rose-500 bg-rose-950" : "border-slate-700 bg-slate-800"}`;
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.add("hidden"), 3500);
};

/* ---------- tempo (o servidor manda UTC; mostramos no fuso do usuário) ---------- */
const utc = (iso) => new Date(iso + (/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? "" : "Z"));
const when = (iso) => (iso ? utc(iso).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
const hms = (iso) => utc(iso).toLocaleTimeString("pt-BR");
const ago = (iso) => { const s = Math.max(0, Math.round((Date.now() - utc(iso)) / 1000)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}min ${s % 60}s`; };

/* ---------- linha do tempo: nós concluído (✓ verde) / em andamento (● pulsando) / falhou (✕) / pendente (vazio) ---------- */
const TL = {
  done: { dot: "bg-emerald-500 border-emerald-500 text-white", text: "text-emerald-400", mark: "✓" },
  active: { dot: "bg-slate-950 border-blue-400 text-blue-300 animate-pulse", text: "text-blue-300", mark: "●" },
  failed: { dot: "bg-rose-500 border-rose-500 text-white", text: "text-rose-400", mark: "✕" },
  pending: { dot: "bg-slate-950 border-slate-600 text-slate-600", text: "text-slate-500", mark: "" },
};
function timeline(steps) {
  const reached = (st) => st !== "pending";
  return `<div class="flex min-w-[640px]">${steps.map((st, i) => {
    const c = TL[st.state] || TL.pending;
    const left = i > 0 && reached(st.state) ? "bg-emerald-500" : "bg-slate-700";
    const right = i < steps.length - 1 && st.state === "done" ? "bg-emerald-500" : "bg-slate-700";
    return `<div class="flex min-w-0 flex-1 flex-col items-center text-center">
      <div class="h-9 px-1 text-[10px] leading-tight text-slate-400"><div class="truncate">${st.actor_name ? esc((st.emoji || "") + " " + st.actor_name) : "&nbsp;"}</div><div class="text-slate-500">${esc(when(st.at))}</div></div>
      <div class="flex w-full items-center"><span class="h-1 flex-1 ${i > 0 ? left : "bg-transparent"}"></span>
        <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 text-xs font-bold ${c.dot}">${c.mark}</span>
        <span class="h-1 flex-1 ${i < steps.length - 1 ? right : "bg-transparent"}"></span></div>
      <div class="mt-1.5 px-1 text-xs font-bold ${c.text}">${esc(st.label)}</div>
      <div class="line-clamp-3 px-1 text-[10px] leading-snug text-slate-500" title="${esc(st.detail)}">${esc(st.detail)}</div></div>`;
  }).join("")}</div>`;
}
const dots = (steps) => `<span class="flex gap-0.5" title="${esc(steps.map((x) => x.label + ": " + x.state).join(" · "))}">${steps.map((x) =>
  `<span class="h-1.5 w-4 rounded-full ${x.state === "done" ? "bg-emerald-500" : x.state === "active" ? "bg-blue-400 animate-pulse" : x.state === "failed" ? "bg-rose-500" : "bg-slate-700"}"></span>`).join("")}</span>`;
const workingOn = (key) => S.agents.find((a) => a.activity?.state === "working" && a.activity.item === key);

const COLS = [
  { id: "backlog", title: "Product Backlog", dot: "bg-slate-400", match: (i) => i.status === "backlog" },
  { id: "sprint", title: "Sprint Backlog", dot: "bg-blue-500", match: (i) => i.status === "sprint" },
  { id: "dev", title: "Em desenvolvimento / QA", dot: "bg-purple-500", match: (i) => ["progress", "qa"].includes(i.status) },
  { id: "done", title: "Concluído", dot: "bg-emerald-500", match: (i) => ["done", "rejected"].includes(i.status) },
];
const ROLE_COLOR = { product_owner: "border-purple-500/40", scrum_master: "border-blue-500/40", developer: "border-emerald-500/40" };
let S = null, tab = "live", chatLog = [], chatCtx = null;

function renderAgents() {
  $("#agents").innerHTML = S.agents.map((a) => {
    const w = a.activity?.state === "working", ac = a.activity || {};
    return `<div class="rounded-xl border ${ROLE_COLOR[a.scrum_role] || "border-slate-700"} bg-slate-950/50 transition hover:bg-slate-900 ${a.enabled ? "" : "opacity-40"}">
    <button data-slug="${esc(a.slug)}" class="flex w-full items-center justify-between p-2.5 text-left">
      <span class="flex items-center gap-2.5"><span class="rounded-lg border border-slate-800 bg-slate-900 p-1.5 text-xl">${esc(a.emoji)}</span>
        <span><b class="block text-xs">${esc(a.name)}</b><span class="text-[10px] text-slate-400">${esc(a.role_label)}${a.specialty ? " · " + esc(a.specialty) : ""}</span></span></span>
      <span class="text-right"><span class="chip">${esc(a.provider)}</span><span class="mt-1 block max-w-[8rem] truncate font-mono text-[9px] text-slate-500">${esc(a.model)}</span></span>
    </button>
    <div class="border-t border-slate-800/70 px-2.5 py-1.5 text-[10px] leading-snug ${w ? "text-blue-300" : "text-slate-500"}">
      ${w ? `<span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span><b>${esc(ac.what)}${ac.item ? " · " + esc(ac.item) : ""}</b> — ${esc(ac.action)} <span class="text-slate-500">· há ${esc(ac.since ? ago(ac.since) : "")}</span> <button data-trace="${esc(a.slug)}" class="ml-1 rounded border border-blue-500/40 px-1.5 text-[10px] text-blue-300 hover:bg-slate-800">🔍 ver tarefa</button>`
        : `<span class="mr-1 inline-block h-2 w-2 rounded-full bg-slate-600"></span>ocioso${ac.last_result ? " · última resposta: " + esc(ac.last_result.slice(0, 70)) : ""} <button data-trace="${esc(a.slug)}" class="ml-1 rounded border border-slate-700 px-1.5 text-[10px] text-slate-400 hover:bg-slate-800">🔍 última tarefa</button>`}
    </div></div>`;
  }).join("");
  document.querySelectorAll("#agents button[data-slug]").forEach((b) => (b.onclick = () => openDrawer(b.dataset.slug)));
  document.querySelectorAll("#agents button[data-trace]").forEach((b) => (b.onclick = () => openTrace(b.dataset.trace)));
}

function renderBoard() {
  $("#board").innerHTML = COLS.map((c) => {
    const items = S.items.filter(c.match);
    return `<div class="flex flex-col gap-2 rounded-2xl border border-slate-800/80 bg-slate-900/40 p-3">
      <div class="flex items-center justify-between border-b border-slate-800 pb-2 text-xs font-bold"><span class="flex items-center gap-2"><span class="h-2 w-2 rounded-full ${c.dot}"></span>${c.title}</span><span class="chip">${items.length}</span></div>
      ${items.map(card).join("") || '<p class="py-6 text-center text-[11px] text-slate-600">vazio</p>'}</div>`;
  }).join("");
}
function card(i) {
  const v = i.qa_verdict, w = workingOn(i.key);
  const badge = i.status === "qa" ? '<span class="chip border-amber-500/50 text-amber-400">QA</span>'
    : i.status === "rejected" ? '<span class="chip border-rose-500/50 text-rose-400">rejeitado</span>'
    : i.status === "done" ? '<span class="chip border-emerald-500/50 text-emerald-400">aceito</span>' : "";
  return `<div data-item="${+i.id}" class="cursor-pointer rounded-xl border ${w ? "border-blue-500/60" : "border-slate-800"} bg-slate-950/80 p-3 shadow transition hover:border-slate-600">
    <div class="flex items-center justify-between font-mono text-[10px]"><b class="text-blue-400">${esc(i.key)}</b><span class="flex items-center gap-1">${i.status === "backlog" ? `<button data-del="${+i.id}" title="Remover do backlog (ex.: duplicata)" class="rounded px-1 text-slate-500 hover:bg-rose-950 hover:text-rose-400">🗑</button>` : ""}${badge}<span class="chip">${esc(i.points)} pts</span></span></div>
    <p class="mt-1.5 text-xs font-semibold leading-snug">${esc(i.title)}</p>
    <div class="mt-2">${dots(i.timeline.steps)}</div>
    ${w ? `<p class="mt-2 text-[10px] text-blue-300"><span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>${esc(w.emoji)} <b>${esc(w.name)}</b> ${esc(w.activity.action)}</p>` : ""}
    ${["progress", "qa"].includes(i.status) ? `<div class="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-900"><div class="h-full bg-gradient-to-r from-blue-500 to-purple-500" style="width:${+i.progress || 0}%"></div></div>` : ""}
    <div class="mt-2 flex items-center justify-between border-t border-slate-900 pt-2 text-[10px] text-slate-400">
      <span>${i.assignee ? esc(i.assignee_emoji) + " " + esc(i.assignee_name) : "sem responsável"}</span>${i.rework_count ? `<span class="text-amber-400">↻ ${+i.rework_count}</span>` : ""}</div>
    ${v && !v.passed ? `<p class="mt-1 text-[10px] text-rose-400">QA: ${esc(v.notes)}</p>` : ""}
  </div>`;
}

/* ---------- detalhe do item com linha do tempo ---------- */
let openItem = null, openFile = null, itemSig = "";
async function loadFiles(i) {
  const box = $("#item-files"); if (!box) return;
  try {
    const { files } = await api(`/items/${i.id}/files`);
    box.innerHTML = files.length ? `<div class="flex flex-wrap gap-2">${files.map((f) => `<button data-file="${esc(f.path)}" class="rounded-lg border ${f.path === openFile ? "border-blue-400 text-blue-300" : "border-slate-700 text-slate-300"} px-2 py-1 font-mono text-[11px] hover:bg-slate-800">📄 ${esc(f.path)} <span class="text-slate-500">${esc(f.bytes)} B</span></button>`).join("")}</div><pre id="file-view" class="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300"></pre>`
      : '<p class="text-xs text-slate-500">Nenhum arquivo ainda — o desenvolvedor grava o entregável na etapa de Desenvolvimento.</p>';
    box.querySelectorAll("[data-file]").forEach((b) => (b.onclick = () => { openFile = b.dataset.file; showFile(i); loadFiles(i); }));
    if (openFile || files.length) { openFile = openFile || files[0].path; showFile(i); }
  } catch (e) { box.textContent = e.message; }
}
async function loadItemGit(i) {
  const box = $("#item-git"); if (!box) return;
  try {
    const { commits } = await api(`/items/${i.id}/git`);
    box.innerHTML = commits.length ? commits.map((c) => `<div data-sha="${esc(c.sha)}" class="flex cursor-pointer gap-2 border-t border-slate-900 py-1 text-xs hover:bg-slate-900/60"><span class="font-mono text-slate-500">${esc(c.short)}</span><span class="w-16 shrink-0 font-bold ${STAGE_CHIP[c.stage] || "text-slate-400"}">${esc(c.stage)}</span><span class="min-w-0 flex-1 truncate">${esc(c.subject)}</span><span class="text-slate-500">${esc(c.author)}</span></div>`).join("") : '<p class="text-xs text-slate-500">Sem commits ainda — o primeiro vem no refinamento.</p>';
    box.querySelectorAll("[data-sha]").forEach((r) => (r.onclick = () => showCommit(r.dataset.sha)));
  } catch (e) { box.textContent = e.message; }
}
async function showFile(i) {
  try { const f = await api(`/items/${i.id}/files/${encodeURI(openFile)}`); const v = $("#file-view"); if (v) v.textContent = f.content; } catch (e) { const v = $("#file-view"); if (v) v.textContent = e.message; }
}
function renderItemModal(force = false) {
  const i = S.items.find((x) => x.id === openItem); if (!i) return;
  const sig = JSON.stringify([i.status, i.timeline, i.qa_verdict, i.notes, workingOn(i.key)?.activity?.action]);
  if (!force && sig === itemSig) return;  // nada mudou: não pisca nem perde o arquivo aberto
  itemSig = sig;
  const w = workingOn(i.key), cur = i.timeline.steps.find((x) => x.key === i.timeline.current);
  $("#item-body").innerHTML = `<div class="mb-1 flex items-center gap-2 font-mono text-xs"><b class="text-blue-400">${esc(i.key)}</b><span class="chip">${esc(i.points)} pts</span><span class="chip">${esc(i.status)}</span>${i.rework_count ? `<span class="chip text-amber-400">↻ ${+i.rework_count} retrabalho(s)</span>` : ""}</div>
    <h3 class="text-lg font-bold">${esc(i.title)}</h3>
    ${w ? `<p class="mt-2 rounded-lg border border-blue-500/40 bg-blue-950/30 p-2 text-xs text-blue-200"><span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>${esc(w.emoji)} <b>${esc(w.name)}</b> está ${esc(w.activity.what)} agora — ${esc(w.activity.action)} (há ${esc(ago(w.activity.since))})</p>`
      : cur && cur.state !== "done" ? `<p class="mt-2 text-xs text-slate-400">Etapa atual: <b class="text-slate-200">${esc(cur.label)}</b>${cur.actor_name ? " — " + esc(cur.actor_name) : ""}</p>` : ""}
    <div class="mt-4 overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/60 p-4">${timeline(i.timeline.steps)}</div>
    <div class="mt-4 grid gap-4 md:grid-cols-2">
      <div><label>Critérios de aceite</label><ul class="list-disc space-y-1 pl-5 text-xs text-slate-300">${(i.acceptance_criteria || []).map((c) => `<li>${esc(c)}</li>`).join("") || "<li>nenhum</li>"}</ul></div>
      <div class="space-y-2 text-xs"><div><label>Responsável</label>${i.assignee ? esc(i.assignee_emoji + " " + i.assignee_name) : "ainda não atribuído"}</div>
        ${i.qa_verdict ? `<div><label>Veredito do QA (${esc(i.qa_verdict.by)})</label><span class="${i.qa_verdict.passed ? "text-emerald-400" : "text-rose-400"}">${i.qa_verdict.passed ? "aprovado" : "reprovado"}</span> — ${esc(i.qa_verdict.notes)}</div>` : ""}
        ${i.notes ? `<div><label>Notas</label>${esc(i.notes)}</div>` : ""}</div></div>
    <div class="mt-4"><label>Entregáveis (resultado produzido pelos agentes)</label><div id="item-files"><p class="text-xs text-slate-500">carregando…</p></div></div>
    <div class="mt-4"><label>Git — branch <code>feature/${esc(i.key)}</code></label><div id="item-git"><p class="text-xs text-slate-500">carregando…</p></div></div>`;
  loadFiles(i); loadItemGit(i);
}
$("#board").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    e.stopPropagation();
    if (!confirm("Remover esta história do Product Backlog?")) return;
    try { await api(`/items/${+del.dataset.del}`, { method: "DELETE" }); toast("História removida"); refresh(); } catch (err) { toast(err.message, true); }
    return;
  }
  const c = e.target.closest("[data-item]"); if (!c) return; openItem = +c.dataset.item; openFile = null; renderItemModal(true); $("#dlg-item").showModal(); });
$("#dlg-item").addEventListener("close", () => { openItem = null; openFile = null; itemSig = ""; });

function renderHeader() {
  const sp = S.sprint;
  $("#sprint-title").textContent = sp ? `${sp.name} · ${sp.status}${sp.refining ? " · 🧭 PO refinando o pedido…" : sp.running ? " · executando…" : ""}` : "Sem sprint — peça algo ao PO";
  $("#sprint-title").className = `text-xs ${sp?.error ? "text-rose-400" : "text-slate-400"}`;
  if (sp?.error) $("#sprint-title").textContent += ` · ⚠ ${sp.error}`;
  $("#m-points").textContent = sp ? `${sp.done_points}/${sp.capacity}` : "0/0";
  $("#m-vel").textContent = S.last_velocity;
  $("#m-audit").textContent = S.audit_events;
  $("#goal").textContent = sp?.goal ? "Meta: " + sp.goal : "";
  $("#retro").textContent = sp?.retro ? "Retro: " + sp.retro : "";
  $("#bar").style.width = sp ? Math.min(100, Math.round((sp.done_points / (sp.capacity || 1)) * 100)) + "%" : "0%";
  const n = S.agents.filter((a) => a.activity?.state === "working").length;
  $("#live-count").innerHTML = n ? `<span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>${n} agente(s) trabalhando agora` : "todos os agentes ociosos";
  $("#sprint-tl").innerHTML = S.sprint_timeline ? timeline(S.sprint_timeline.steps.map((x) => ({ ...x, actor_name: x.actor_name, emoji: x.emoji }))) : '<p class="text-xs text-slate-500">Peça algo ao PO para iniciar uma sprint.</p>';
  const w = S.worker || { alive: true, pending: 0 };
  const warn = $("#worker-warn");
  warn.classList.toggle("hidden", w.alive);
  if (!w.alive) warn.innerHTML = `<b>Nenhum worker ativo.</b> ${w.pending ? `Há <b>${+w.pending}</b> job(s) esperando na fila (refinamento, sprint, CI ou deploy). ` : ""}Rode <code class="rounded bg-black/40 px-1">uv run flask --app flow worker</code> em outro terminal (ou deixe <code>[worker] embedded = true</code> no flow.toml e reinicie o servidor).`;
  const live = (S.deployments || []).find((d) => d.status === "running");
  $("#m-live").classList.toggle("hidden", !live);
  if (live) { const a = $("#m-live-a"); a.textContent = live.app; a.href = live.url; }
  const b = $("#btn-run");
  b.textContent = sp?.running ? "■ Pausar Sprint" : sp?.refining ? "⏳ PO refinando…" : sp?.status === "done" ? "▶ Nova sprint com o backlog" : "▶ Executar Sprint";
  b.disabled = !!sp?.refining; b.title = sp?.refining ? "Aguarde o PO terminar de criar as user stories" : "";
  b.className = `rounded-lg px-3 py-2 text-xs font-bold ${sp?.running ? "bg-rose-600 hover:bg-rose-500" : sp?.refining ? "bg-slate-700 opacity-60 cursor-not-allowed" : "bg-emerald-600 hover:bg-emerald-500"}`;
}

const pendingDeploys = () => (S?.deployments || []).filter((d) => d.status === "awaiting_approval").length;
const TABS = () => [["live", "Ao vivo"], ["git", "Git"], ["deploy", "Deploy" + (pendingDeploys() ? ` ● ${pendingDeploys()}` : "")], ["audit", "Auditoria"], ["a2a", "Tasks A2A"], ["chat", "Chat com PO"]];
function renderTabs() {
  $("#tabs").innerHTML = TABS().map(([id, t]) => `<button data-t="${id}" class="rounded-t-lg px-3 py-1.5 ${tab === id ? "bg-slate-900 text-white" : "text-slate-500 hover:text-slate-300"}">${t}</button>`).join("");
  document.querySelectorAll("#tabs button").forEach((b) => (b.onclick = () => { tab = b.dataset.t; panelSig = ""; renderTabs(); renderPanel(); }));
}
const SC = { ok: "text-emerald-400", denied: "text-rose-400", error: "text-rose-400" };
async function renderPanel() {
  const p = $("#panel");
  if (tab === "live") {
    const [d, jq] = await Promise.all([api("/activity"), api("/jobs?limit=8")]);
    p.className = "h-80 overflow-y-auto p-3 text-xs leading-relaxed";
    p.innerHTML = `<table class="mb-4 w-full text-left"><thead class="text-[10px] uppercase text-slate-500"><tr><th class="pb-1">Agente</th><th>Situação</th><th>Fazendo</th><th>Há</th></tr></thead><tbody>${S.agents.map((a) => {
      const x = d.agents[a.slug] || {}; const w = x.state === "working";
      return `<tr class="border-t border-slate-900"><td class="py-1.5">${esc(a.emoji)} <b>${esc(a.name)}</b></td>
        <td>${w ? '<span class="text-emerald-400"><span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>trabalhando</span>' : '<span class="text-slate-500">ocioso</span>'}</td>
        <td class="text-slate-300">${w ? `<b>${esc(x.what)}</b>${x.item ? " · " + esc(x.item) : ""} — ${esc(x.action)}` : '<span class="text-slate-600">—</span>'}</td>
        <td class="text-slate-500">${w && x.since ? esc(ago(x.since)) : ""} <button data-trace="${esc(a.slug)}" class="ml-1 rounded border border-slate-700 px-1.5 text-[10px] text-blue-300 hover:bg-slate-800">🔍</button></td></tr>`;
    }).join("")}</tbody></table>
    <h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Fila de jobs (worker ${jq.worker.alive ? `ativo ×${+jq.worker.workers}` : '<span class="text-amber-400">parado</span>'})</h4>
    <div class="mb-4 font-mono text-[11px]">${jq.jobs.map((j) => `<div class="flex gap-2"><span class="text-slate-600">#${+j.id}</span><span class="w-24 text-slate-300">${esc(j.task)}</span><span class="w-20 ${j.status === "succeeded" ? "text-emerald-400" : j.status === "failed" ? "text-rose-400" : j.status === "doing" ? "text-blue-300" : "text-amber-300"}">${esc(j.status)}</span><span class="text-slate-500">tent. ${+j.attempts} · ${esc(JSON.stringify(j.args)).slice(0, 90)}</span></div>`).join("") || '<span class="text-slate-600">fila vazia</span>'}</div>
    <h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Últimos acontecimentos</h4>
    ${d.feed.map((f) => `<div class="flex gap-2"><span class="shrink-0 font-mono text-slate-600">${esc(hms(f.at))}</span><span class="${f.status === "ok" ? "text-slate-300" : "text-rose-400"}">${esc(f.emoji)} <b>${esc(f.actor_name)}</b> ${esc(f.text)}</span></div>`).join("") || '<span class="text-slate-600">nada ainda</span>'}`;
    p.querySelectorAll("button[data-trace]").forEach((b) => (b.onclick = () => openTrace(b.dataset.trace)));
    return;
  }
  if (tab === "git") return renderGit(p);
  if (tab === "deploy") return renderDeploy(p);
  p.className = "h-80 overflow-y-auto p-3 font-mono text-[11px] leading-relaxed";
  if (tab === "audit") {
    const ev = await api("/audit?limit=80");
    p.innerHTML = ev.map((e) => `<div class="flex gap-2"><span class="shrink-0 text-slate-600">${esc(hms(e.created_at))}</span>
      <span class="shrink-0 text-blue-400">${esc(e.actor)}</span><span class="${SC[e.status] || ""}">${esc(e.event_type)}${e.target ? "→" + esc(e.target) : ""}</span>
      <span class="truncate text-slate-500" title="${esc(JSON.stringify(e.payload))}">${esc(JSON.stringify(e.payload)).slice(0, 140)}</span></div>`).join("") || "sem eventos";
  } else if (tab === "a2a") {
    const t = await api("/a2a/tasks");
    p.innerHTML = t.map((x) => `<div class="flex gap-3"><span class="text-slate-600">${esc(hms(x.created_at))}</span><span class="text-blue-400">${esc(x.agent)}</span>
      <span class="${x.state.endsWith("COMPLETED") ? "text-emerald-400" : x.state.endsWith("FAILED") ? "text-rose-400" : "text-amber-400"}">${esc(x.state.replace("TASK_STATE_", ""))}</span>
      <span class="text-slate-500">${esc(x.steps ?? "")} passos · ${esc(x.tokensIn ?? 0)}/${esc(x.tokensOut ?? 0)} tok</span></div>`).join("") || "sem tasks";
  } else {
    p.innerHTML = `<div class="flex h-full flex-col"><div id="chatlog" class="flex-1 space-y-1 overflow-y-auto">${chatLog.map((m) => `<p><b class="${m.me ? "text-blue-400" : "text-emerald-400"}">${m.me ? "você" : "PO"}:</b> ${esc(m.t)}</p>`).join("")}</div>
      <form id="chatform" class="mt-2 flex gap-2"><input id="chatin" class="flex-1" placeholder="Converse com o Product Owner (A2A)…"><button class="rounded-lg bg-blue-600 px-3 text-xs font-bold">Enviar</button></form></div>`;
    $("#chatform").onsubmit = async (e) => {
      e.preventDefault(); const t = $("#chatin").value.trim(); if (!t) return;
      chatLog.push({ me: true, t }); renderPanel();
      try { const r = await api("/chat", { method: "POST", body: { message: t, context_id: chatCtx } }); chatCtx = r.task.contextId; chatLog.push({ t: r.reply }); }
      catch (err) { chatLog.push({ t: "erro: " + err.message }); }
      renderPanel();
    };
  }
}


/* ---------- aba Git: branches, tags e commits (1 por etapa) ---------- */
let panelSig = "", gitBranch = "", gitData = null, gitKey = "";
const STAGE_CHIP = { refine: "text-purple-300", plan: "text-blue-300", implement: "text-sky-300", verify: "text-amber-300", accept: "text-emerald-300", merge: "text-emerald-300", reject: "text-rose-300", review: "text-indigo-300", release: "text-emerald-400", init: "text-slate-400" };
const KIND_ICON = { main: "★", sprint: "◆", story: "⎇", backlog: "☰" };
async function renderGit(p) {
  const key = `${S.audit_events}|${gitBranch}`;
  if (key !== gitKey) {  // só consulta o git quando algo novo foi auditado (cada commit gera um evento)
    gitKey = key;
    try {
      const [st, br, lg] = await Promise.all([api("/git/status"), api("/git/branches"), api(`/git/log?limit=80${gitBranch ? "&branch=" + encodeURIComponent(gitBranch) : ""}`)]);
      gitData = { st, ...br, commits: lg.commits };
    } catch (e) { gitData = { error: e.message }; }
  }
  const d = gitData, sig = "git" + JSON.stringify(d) + gitBranch;
  if (sig === panelSig) return; panelSig = sig;
  p.className = "h-80 overflow-y-auto p-3 text-xs leading-relaxed";
  if (!d || d.error) { p.textContent = d?.error || "…"; return; }
  if (!d.st.enabled) { p.innerHTML = `<p class="text-slate-400">${esc(d.st.reason || "git desativado")}. Ative com <code>[git] enabled = true</code> no flow.toml.</p>`; return; }
  if (!d.st.initialized) { p.innerHTML = '<p class="text-slate-400">O repositório é criado no primeiro refinamento/sprint (workspace/).</p>'; return; }
  p.innerHTML = `<div class="mb-2 flex flex-wrap gap-2 text-[10px]"><span class="chip">HEAD ${esc(d.st.head)}</span><span class="chip">${+d.st.commits} commits</span><span class="chip">${+d.st.branches} branches</span><span class="chip">${+d.st.tags} tags</span><span class="chip" title="${esc(d.st.path)}">${esc(d.st.path)}</span></div>
  <div class="grid gap-3 md:grid-cols-[16rem_1fr]">
    <div><h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Branches</h4>
      <button data-br="" class="block w-full truncate rounded px-1 text-left ${gitBranch === "" ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"}">todas</button>
      ${d.branches.map((b) => `<button data-br="${esc(b.name)}" title="${esc(b.subject)}" class="flex w-full items-center justify-between gap-1 truncate rounded px-1 text-left ${gitBranch === b.name ? "bg-slate-800 text-white" : "text-slate-400 hover:text-slate-200"}"><span class="truncate">${KIND_ICON[b.kind] || "·"} ${esc(b.name)}</span>${b.kind === "story" || b.kind === "sprint" ? (b.merged ? '<span class="text-emerald-500">mergeada</span>' : '<span class="text-amber-400">aberta</span>') : ""}</button>`).join("")}
      <h4 class="mb-1 mt-3 text-[10px] font-bold uppercase text-slate-500">Tags</h4>
      ${d.tags.map((t) => `<div class="truncate text-slate-400" title="${esc(t.subject)}">🏷 ${esc(t.name)} <span class="text-slate-600">${esc(t.sha)}</span></div>`).join("") || '<span class="text-slate-600">nenhuma</span>'}</div>
    <div class="min-w-0"><h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Commits ${gitBranch ? "em " + esc(gitBranch) : "(todas as branches)"}</h4>
      ${d.commits.map((c) => `<div data-sha="${esc(c.sha)}" class="flex cursor-pointer items-baseline gap-2 border-t border-slate-900 py-1 hover:bg-slate-900/60"><span class="shrink-0 font-mono text-slate-500">${esc(c.short)}</span>
        <span class="w-16 shrink-0 font-bold ${STAGE_CHIP[c.stage] || "text-slate-400"}">${esc(c.stage || "·")}</span><span class="min-w-0 flex-1 truncate text-slate-200" title="${esc(c.subject)}">${esc(c.subject)}</span>
        ${c.refs.map((r) => `<span class="chip shrink-0 ${r.startsWith("tag:") ? "text-amber-300" : "text-blue-300"}">${esc(r.replace("tag: ", "🏷 "))}</span>`).join("")}
        <span class="hidden shrink-0 text-slate-500 md:inline">${esc(c.author)}</span><span class="shrink-0 text-slate-600">${esc(when(c.date))}</span></div>`).join("") || '<span class="text-slate-600">nenhum commit</span>'}</div></div>`;
  p.querySelectorAll("[data-br]").forEach((b) => (b.onclick = () => { gitBranch = b.dataset.br; renderPanel(); }));
  p.querySelectorAll("[data-sha]").forEach((r) => (r.onclick = () => showCommit(r.dataset.sha)));
}
async function showCommit(sha) {
  try {
    const c = await api(`/git/commits/${encodeURIComponent(sha)}`);
    const diff = esc(c.diff).split("\n").map((l) => l.startsWith("+") && !l.startsWith("+++") ? `<span class="text-emerald-400">${l}</span>` : l.startsWith("-") && !l.startsWith("---") ? `<span class="text-rose-400">${l}</span>` : l.startsWith("@@") ? `<span class="text-sky-400">${l}</span>` : l).join("\n");
    $("#commit-body").innerHTML = `<div class="flex flex-wrap items-center gap-2 font-mono text-xs"><b class="text-blue-400">${esc(c.short)}</b>${c.stage ? `<span class="chip">${esc(c.stage)}</span>` : ""}${c.story ? `<span class="chip">${esc(c.story)}</span>` : ""}${(c.refs || []).map((r) => `<span class="chip">${esc(r)}</span>`).join("")}</div>
      <h3 class="mt-1 text-base font-bold">${esc(c.subject)}</h3><p class="text-xs text-slate-400">${esc(c.author)} &lt;${esc(c.email)}&gt; · ${esc(when(c.date))}</p>
      <pre class="mt-2 whitespace-pre-wrap rounded-lg border border-slate-800 bg-slate-950 p-2 text-[11px] text-slate-400">${esc(c.stat || "(sem alterações de arquivo — commit de registro da etapa)")}</pre>
      <pre class="mt-2 max-h-80 overflow-auto rounded-lg border border-slate-800 bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-300">${diff || "(vazio)"}${c.truncated ? "\n… diff truncado" : ""}</pre>`;
    $("#dlg-commit").showModal();
  } catch (e) { toast(e.message, true); }
}

/* ---------- aba Deploy: CI/CD local (agente devops) ---------- */
let logsFor = null, logText = "";
const DEP = { awaiting_approval: ["aguardando aprovação", "text-amber-300"], ci_pending: ["CI na fila", "text-blue-300"], ci_running: ["CI rodando", "text-blue-300"], ci_failed: ["CI falhou", "text-rose-400"],
  deploying: ["subindo", "text-blue-300"], running: ["no ar", "text-emerald-400"], failed: ["falhou", "text-rose-400"], stopped: ["parada", "text-slate-400"], skipped: ["nada a subir", "text-slate-500"] };
async function renderDeploy(p) {
  if (logsFor) { try { logText = (await api(`/deployments/${logsFor}/logs?lines=60`)).log; } catch (e) { logText = e.message; } }
  const sig = "dep" + JSON.stringify(S.deployments) + logsFor + logText;
  if (sig === panelSig) return; panelSig = sig;
  p.className = "h-80 overflow-y-auto p-3 text-xs leading-relaxed";
  const ds = S.deployments || [];
  if (!ds.length) { p.innerHTML = '<p class="text-slate-400">Nenhuma release ainda. Ao fim de uma sprint, a tag <code>sprint-N</code> vai para o agente 🚀 DevOps: ele roda o CI e, com a sua aprovação, sobe a aplicação desta máquina. A história precisa entregar um <code>flow-app.toml</code> (skill <i>app-manifest</i>).</p>'; return; }
  p.innerHTML = ds.map((d) => {
    const [label, cls] = DEP[d.status] || [d.status, ""];
    const canApprove = ["awaiting_approval", "stopped", "failed"].includes(d.status) && d.ci_report.length && d.ci_report.every((r) => r.ok);
    const live = ["running", "deploying"].includes(d.status);
    return `<div class="mb-2 rounded-xl border ${d.status === "awaiting_approval" ? "border-amber-500/50" : "border-slate-800"} bg-slate-950/60 p-3">
      <div class="flex flex-wrap items-center gap-2"><b>🚀 ${esc(d.app === "-" ? "Release" : d.app)}</b><span class="chip">${esc(d.tag)} · ${esc(d.commit)}</span><span class="font-bold ${cls}">${d.status === "running" ? '<span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>' : ""}${esc(label)}</span>
        ${d.url && d.status === "running" ? `<a href="${esc(d.url)}" target="_blank" rel="noopener" class="font-mono text-emerald-300 underline">${esc(d.url)}</a>` : ""}
        <span class="ml-auto flex gap-1">${canApprove ? `<button data-dp="approve" data-id="${+d.id}" class="rounded-lg bg-emerald-600 px-2.5 py-1 font-bold hover:bg-emerald-500">${d.status === "awaiting_approval" ? "Aprovar e subir" : "Subir de novo"}</button>` : ""}
          ${live ? `<button data-dp="stop" data-id="${+d.id}" class="rounded-lg bg-rose-600 px-2.5 py-1 font-bold hover:bg-rose-500">Parar</button>` : ""}
          ${d.status !== "skipped" && !live ? `<button data-dp="ci" data-id="${+d.id}" class="rounded-lg border border-slate-700 px-2.5 py-1 hover:bg-slate-800">Rodar CI</button>` : ""}
          ${d.port ? `<button data-dp="logs" data-id="${+d.id}" class="rounded-lg border border-slate-700 px-2.5 py-1 hover:bg-slate-800">${logsFor === d.id ? "Ocultar logs" : "Logs"}</button>` : ""}</span></div>
      <p class="mt-1 text-slate-400">${esc(d.detail)}</p>
      ${d.ci_report.length ? `<div class="mt-1 flex flex-wrap gap-2 font-mono text-[11px]">${d.ci_report.map((r) => `<span class="${r.ok ? "text-emerald-400" : "text-rose-400"}" title="${esc(r.cmd + "\n" + r.output)}">${r.ok ? "✓" : "✕"} ${esc(r.name)} <span class="text-slate-600">${esc(r.seconds)}s</span></span>`).join("")}</div>` : ""}
      ${logsFor === d.id ? `<pre class="mt-2 max-h-40 overflow-auto rounded-lg border border-slate-800 bg-black p-2 text-[11px] text-slate-300">${esc(logText) || "(sem saída)"}</pre>` : ""}</div>`;
  }).join("");
  p.querySelectorAll("[data-dp]").forEach((b) => (b.onclick = async () => {
    const id = +b.dataset.id, act = b.dataset.dp;
    if (act === "logs") { logsFor = logsFor === id ? null : id; panelSig = ""; return renderPanel(); }
    if (act === "approve" && !confirm("Aprovar a subida?\n\nO código desta release foi escrito por agentes (LLM) e vai ser EXECUTADO nesta máquina, com as suas permissões, em 127.0.0.1. Revise os commits na aba Git antes, se quiser.")) return;
    try { const r = await api(`/deployments/${id}/${act}`, { method: "POST" }); toast(act === "approve" ? "Aprovado — o agente devops está subindo a aplicação" : act === "ci" ? "CI solicitado ao agente devops" : r.result || "ok"); setTimeout(refresh, 800); }
    catch (e) { toast(e.message, true); }
  }));
}

async function refresh() {
  try { S = await api("/state"); renderHeader(); renderTabs(); renderAgents(); renderBoard(); if (openItem) renderItemModal(); if (tab !== "chat") renderPanel(); } catch (e) { toast(e.message, true); }
}

/* ---------- ações ---------- */
$("#btn-ask").onclick = () => $("#dlg-ask").showModal();
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => b.closest("dialog").close()));
$("#ask-send").onclick = async () => {
  const request = $("#ask-text").value.trim(); if (!request) return;
  try { await api("/refine", { method: "POST", body: { request } }); $("#ask-text").value = ""; $("#dlg-ask").close(); toast("PO refinando o pedido…"); setTimeout(refresh, 1200); }
  catch (e) { toast(e.message, true); }
};
$("#btn-run").onclick = async () => {
  const sp = S?.sprint; if (!sp) return toast("Peça algo ao PO primeiro", true);
  try {
    let id = sp.id;
    if (sp.status === "done") id = (await api("/sprints", { method: "POST", body: { capacity: sp.capacity } })).id;  // sprint anterior encerrada: abre a próxima
    await api(`/sprints/${id}/${sp.running ? "stop" : "run"}`, { method: "POST" }); toast(sp.running ? "Pausa solicitada" : "Sprint iniciada"); refresh();
  }
  catch (e) { toast(e.message, true); }
};
$("#btn-verify").onclick = async () => {
  const r = await api("/audit/verify");
  $("#m-chain").innerHTML = r.ok ? `<span class="text-emerald-400"> ✓ íntegra</span>` : `<span class="text-rose-400"> ✗ adulterada (#${esc(r.first_invalid)})</span>`;
};

/* ---------- drawer de configuração do agente ---------- */
const PROVIDERS_HELP = { mock: "offline, determinístico (demo/testes)", ollama: "modelos locais", openai: "OpenAI-compatível (LM Studio, vLLM…)", anthropic: "API Anthropic" };
async function openDrawer(slug) {
  const [a, tools, models] = await Promise.all([api(`/agents/${slug}`), api("/tools"), api("/models").catch(() => ({ models: [], providers: Object.keys(PROVIDERS_HELP) }))]);
  const others = S.agents.filter((x) => x.slug !== slug);
  const d = $("#drawer"); d.classList.remove("hidden");
  d.innerHTML = `<div class="mb-4 flex items-center justify-between"><h3 class="text-lg font-bold">${esc(a.emoji)} ${esc(a.name)} <span class="chip">${esc(a.role_label)}</span></h3><button id="dclose" class="text-2xl text-slate-400">×</button></div>
  <form id="cfg" class="space-y-4 text-sm">
    <div class="grid grid-cols-2 gap-3">
      <div><label>Provedor</label><select name="provider" class="w-full">${models.providers.map((p) => `<option ${p === a.provider ? "selected" : ""} value="${esc(p)}">${esc(p)}</option>`).join("")}</select></div>
      <div><label>Modelo ${models.models.length ? "(Ollama local)" : ""}</label><input name="model" list="mlist" class="w-full" value="${esc(a.model)}"><datalist id="mlist">${models.models.map((m) => `<option value="${esc(m)}">`).join("")}</datalist></div>
      <div class="col-span-2"><label>Base URL (vazio = padrão do provedor)</label><input name="base_url" class="w-full" value="${esc(a.base_url)}" placeholder="http://localhost:11434"></div>
      <div><label>Temperatura</label><input name="temperature" type="number" step="0.1" min="0" max="2" class="w-full" value="${esc(a.temperature)}"></div>
      <div><label>Máx. passos (tool loop)</label><input name="max_steps" type="number" min="1" class="w-full" value="${esc(a.max_steps)}"></div>
    </div>
    <div><label>Prompt do sistema</label><textarea name="system_prompt" rows="4" class="w-full">${esc(a.system_prompt)}</textarea></div>
    <div><label>Tools habilitadas</label><div class="grid max-h-44 grid-cols-2 gap-1 overflow-y-auto rounded-lg border border-slate-800 p-2">${tools.map((t) => `<label class="!mb-0 flex items-start gap-1.5 !normal-case !tracking-normal" title="${esc(t.description)}"><input type="checkbox" class="mt-0.5" name="tool" value="${esc(t.name)}" ${a.tools.includes(t.name) ? "checked" : ""}><span class="text-[11px] font-normal text-slate-300">${esc(t.name)}${t.roles.length ? ' <i class="text-slate-500">(' + esc(t.roles.join("/")) + ")</i>" : ""}</span></label>`).join("")}</div></div>
    <div><label>skills_path (pastas separadas por “;”)</label><input name="skills_path" class="w-full" value="${esc(a.skills_path)}">
      <p class="mt-1 text-[11px] text-slate-500">Encontradas: ${a.skills_found.map((s) => `<span class="chip">${esc(s.name)}</span>`).join(" ") || "nenhuma"}</p></div>
    <div><label>Contexto (JSON: max_tokens, history_messages, memory_k, include_shared_memory, context_paths)</label><textarea name="context" rows="5" class="w-full font-mono text-xs">${esc(JSON.stringify(a.context, null, 2))}</textarea></div>
    <div><label>Servidores MCP externos (JSON: {"nome": {"url": "http://…/mcp"}})</label><textarea name="mcp_servers" rows="3" class="w-full font-mono text-xs">${esc(JSON.stringify(a.mcp_servers, null, 2))}</textarea></div>
    <div><label>Pode delegar via A2A a (hierarquia)</label><div class="flex flex-wrap gap-3">${others.map((o) => `<label class="!mb-0 flex items-center gap-1 !normal-case !tracking-normal"><input type="checkbox" name="deleg" value="${esc(o.slug)}" ${a.can_delegate_to.includes(o.slug) ? "checked" : ""}> <span class="text-xs font-normal">${esc(o.emoji)} ${esc(o.slug)}</span></label>`).join("")}</div></div>
    <div><label>URL A2A remota (vazio = agente local)</label><input name="a2a_url" class="w-full" value="${esc(a.a2a_url)}"></div>
    <label class="!mb-0 flex items-center gap-2 !normal-case"><input type="checkbox" name="enabled" ${a.enabled ? "checked" : ""}> <span class="text-sm font-normal">Agente habilitado</span></label>
    <div class="flex items-center justify-between"><a class="text-xs text-blue-400 underline" target="_blank" href="/a2a/${esc(a.slug)}/.well-known/agent-card.json">Agent Card (A2A)</a><button class="rounded-lg bg-blue-600 px-4 py-2 text-xs font-bold hover:bg-blue-500">Salvar</button></div>
  </form>
  <h4 class="mb-2 mt-6 text-sm font-bold">Memória (PostgreSQL)</h4>
  <div class="space-y-1 text-xs">${a.memories.map((m) => `<p class="rounded border border-slate-800 p-2"><span class="chip">${esc(m.kind)}${m.scope === "shared" ? "/time" : ""}</span> ${esc(m.content)}</p>`).join("") || '<p class="text-slate-500">vazia</p>'}</div>
  <form id="memform" class="mt-2 flex gap-2"><input name="content" class="flex-1" placeholder="Adicionar memória…"><button class="rounded-lg border border-slate-700 px-3 text-xs">+</button></form>`;
  $("#dclose").onclick = () => d.classList.add("hidden");
  $("#cfg").onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target);
    try {
      const body = { provider: f.get("provider"), model: f.get("model"), base_url: f.get("base_url"), temperature: parseFloat(f.get("temperature")),
        max_steps: parseInt(f.get("max_steps"), 10), system_prompt: f.get("system_prompt"), tools: f.getAll("tool"), skills_path: f.get("skills_path"),
        context: JSON.parse(f.get("context") || "{}"), mcp_servers: JSON.parse(f.get("mcp_servers") || "{}"), can_delegate_to: f.getAll("deleg"),
        a2a_url: f.get("a2a_url"), enabled: f.get("enabled") === "on" };
      await api(`/agents/${slug}`, { method: "PATCH", body }); toast("Configuração salva (auditada)"); refresh();
    } catch (err) { toast(err.message, true); }
  };
  $("#memform").onsubmit = async (e) => { e.preventDefault(); const c = new FormData(e.target).get("content"); if (!c) return;
    await api(`/agents/${slug}/memory`, { method: "POST", body: { content: c } }); openDrawer(slug); };
}

/* ---------- detalhe da tarefa: passo a passo ao vivo de um agente (modelo, arquivos, instalação, testes) ---------- */
const TR = { slug: "", sel: "latest", run: "", cursor: "", rows: new Map(), els: new Map(), open: new Map(), timer: null, follow: true, last: null };
const TR_ICON = { task: "📋", think: "🧠", tool: "🛠️", phase: "⏳", test: "🧪", final: "🏁", error: "💥" };
const kb = (n) => (n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`);
const dur = (ms) => (ms == null ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const EXT_CLS = { py: "text-yellow-300", html: "text-orange-300", css: "text-sky-300", js: "text-amber-300", sql: "text-fuchsia-300", toml: "text-lime-300", md: "text-slate-300", txt: "text-slate-300", json: "text-emerald-300" };

function modelView(raw, running) {  // JSON do modelo -> leitura humana (o conteúdo do arquivo com quebras de linha reais)
  try {
    const o = JSON.parse(raw);
    if (o.tool) { const { content, ...rest } = o.args || {}; return `→ ${o.tool}(${Object.keys(rest).length ? JSON.stringify(rest) : ""})` + (typeof content === "string" ? `\n\n── conteúdo ──\n${content}` : ""); }
    if ("final" in o) return `✔ final: ${typeof o.final === "string" ? o.final : JSON.stringify(o.final)}`;
  } catch { /* parcial ou fora do formato */ }
  return running ? raw.replace(/\\n/g, "\n").replace(/\\"/g, '"') : raw;
}
function trDefaultOpen(r) { return r.state === "running" || r.state === "error"; }  // o que está em curso e o que falhou já vêm abertos
function trRow(r) {
  const m = r.meta || {}, run = r.state === "running";
  const st = run ? '<span class="inline-block h-2.5 w-2.5 animate-pulse rounded-full bg-emerald-400"></span>' : r.state === "error" ? '<span class="text-rose-400">✕</span>' : '<span class="text-emerald-400">✓</span>';
  const chips = [
    m.name ? `<span class="chip ${EXT_CLS[m.ext] || "text-slate-300"}">${esc(m.name)}</span>` : "",
    m.ext ? `<span class="chip">.${esc(m.ext)}</span>` : "",
    m.action ? `<span class="chip text-blue-300">${esc(m.action)}</span>` : "",
    m.bytes != null ? `<span class="chip">${kb(m.bytes)} · ${+m.lines} linhas</span>` : "",
    m.tokens_out ? `<span class="chip">${+m.tokens_in}→${+m.tokens_out} tok</span>` : "",
    m.chars && run ? `<span class="chip text-emerald-300">${+m.chars} caracteres gerados…</span>` : "",
    m.isolation ? `<span class="chip ${/SEM/.test(m.isolation) ? "text-amber-300" : "text-emerald-300"}">${esc(m.isolation)}</span>` : "",
    r.ms != null ? `<span class="chip">${dur(r.ms)}</span>` : "",
  ].join(" ");
  let body = r.body, label = "enviado";
  if (r.kind === "think") { body = modelView(r.body, run); label = run ? "o modelo está gerando…" : "resposta do modelo"; }
  else if (r.kind === "tool" && m.tool === "write_workspace_file") label = `conteúdo escrito em ${m.path || ""}`;
  else if (r.kind === "task") label = "tarefa recebida";
  else if (r.kind === "final") label = "resposta final";
  const hasBody = (body || "").trim(), hasRes = (r.result || "").trim();
  const open = TR.open.has(r.id) ? TR.open.get(r.id) : trDefaultOpen(r);
  const pre = (t, cls = "") => `<pre class="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-slate-800 bg-black p-2 text-[11px] leading-snug ${cls}">${esc(t)}${run ? '<span class="animate-pulse text-emerald-400">▍</span>' : ""}</pre>`;
  const details = hasBody || hasRes ? `<details data-rid="${r.id}" ${open ? "open" : ""} class="mt-1"><summary class="cursor-pointer select-none text-[10px] text-slate-500 hover:text-slate-300">detalhes</summary>
      ${hasBody ? `<div class="mt-1 text-[10px] uppercase text-slate-500">${esc(label)}</div>${pre(body, "text-slate-200")}` : ""}
      ${hasRes ? `<div class="mt-1 text-[10px] uppercase text-slate-500">${r.kind === "test" ? "saída" : "resultado"}</div>${pre(r.result, r.state === "error" ? "text-rose-300" : "text-slate-300")}` : ""}</details>` : "";
  return `<div class="flex gap-2"><span class="mt-0.5 shrink-0 font-mono text-[10px] text-slate-600">${esc(hms(r.at))}</span><span class="shrink-0">${TR_ICON[r.kind] || "•"}</span>
    <div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-1.5">${st}<b class="${r.state === "error" ? "text-rose-300" : run ? "text-emerald-300" : "text-slate-100"}">${esc(r.title)}</b>${chips}</div>${details}</div></div>`;
}
function trSide() {
  const rows = [...TR.rows.values()];
  const files = new Map();
  rows.filter((r) => r.meta?.path && r.meta.action).forEach((r) => files.set(r.meta.path + "|" + r.meta.action, r));
  const think = rows.filter((r) => r.kind === "think"), tok = think.reduce((a, r) => a + (r.meta.tokens_out || 0), 0);
  const phases = rows.filter((r) => r.kind === "phase" || r.kind === "test").slice(-8);
  const a = TR.last?.agent;
  $("#tr-side").innerHTML = `
    <div class="mb-3 grid grid-cols-3 gap-1.5 text-center">
      <div class="rounded-lg border border-slate-800 p-1.5"><b class="block text-base">${think.length}<span class="text-xs text-slate-500">/${+(a?.max_steps || 0)}</span></b><span class="text-[10px] text-slate-500">passos do modelo</span></div>
      <div class="rounded-lg border border-slate-800 p-1.5"><b class="block text-base">${[...files.values()].filter((r) => r.meta.action === "escreveu").length}</b><span class="text-[10px] text-slate-500">arquivos escritos</span></div>
      <div class="rounded-lg border border-slate-800 p-1.5"><b class="block text-base">${tok}</b><span class="text-[10px] text-slate-500">tokens gerados</span></div></div>
    <h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Arquivos</h4>
    <div class="mb-3 space-y-1">${[...files.values()].map((r) => `<button data-goto="${r.id}" class="flex w-full items-center gap-1.5 rounded border border-slate-800 px-1.5 py-1 text-left hover:bg-slate-800">
        <span>${r.meta.action === "escreveu" ? "✏️" : r.meta.action === "leu" ? "👁️" : "📂"}</span><span class="truncate font-mono text-[11px] ${EXT_CLS[r.meta.ext] || ""}" title="${esc(r.meta.path)}">${esc(r.meta.name || r.meta.path)}</span>
        <span class="ml-auto shrink-0 text-[10px] text-slate-500">${esc(r.meta.action)}${r.meta.bytes != null ? " · " + kb(r.meta.bytes) : ""}</span></button>`).join("") || '<p class="text-[11px] text-slate-600">nenhum arquivo ainda</p>'}</div>
    <h4 class="mb-1 text-[10px] font-bold uppercase text-slate-500">Sandbox · dependências · testes</h4>
    <div class="space-y-1">${phases.map((r) => `<button data-goto="${r.id}" class="flex w-full items-start gap-1.5 rounded border border-slate-800 px-1.5 py-1 text-left text-[11px] hover:bg-slate-800">
        <span>${r.state === "running" ? "⏳" : r.state === "error" ? "❌" : "✅"}</span><span class="${r.state === "error" ? "text-rose-300" : "text-slate-300"}">${esc(r.title)}</span></button>`).join("") || '<p class="text-[11px] text-slate-600">sem fase de sandbox nesta tarefa</p>'}</div>`;
  $("#tr-side").querySelectorAll("[data-goto]").forEach((b) => (b.onclick = () => { const el = TR.els.get(+b.dataset.goto); if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); el.classList.add("bg-slate-800"); setTimeout(() => el.classList.remove("bg-slate-800"), 1200); } }));
}
function trHead() {
  const d = TR.last; if (!d) return;
  const run = d.run, a = d.agent, ac = a.activity || {};
  const state = run ? (run.state === "running" ? (ac.state === "working" ? "working" : "stale") : run.state) : "none";
  const badge = { working: ["trabalhando", "text-emerald-300 border-emerald-500/50"], ok: ["concluída", "text-emerald-300 border-emerald-500/40"], error: ["falhou", "text-rose-300 border-rose-500/50"], stale: ["sem sinal (interrompida?)", "text-amber-300 border-amber-500/50"], none: ["sem execuções", "text-slate-400 border-slate-700"] }[state];
  $("#tr-head").innerHTML = `<span class="text-2xl">${esc(a.emoji)}</span><div class="min-w-0"><b>${esc(a.name)}</b> <span class="chip">${esc(a.model)}</span>
      <div class="truncate text-[11px] text-slate-400">${run ? esc(run.title) : ""}${state === "working" && ac.action ? " — " + esc(ac.action) : ""}</div></div>
    <span class="ml-auto rounded-full border px-2 py-0.5 text-[11px] font-bold ${badge[1]}">${state === "working" ? '<span class="mr-1 inline-block h-2 w-2 animate-pulse rounded-full bg-emerald-400"></span>' : ""}${badge[0]}</span>
    <span class="font-mono text-[11px] text-slate-500">${run ? "há " + esc(ago(run.started)) : ""}</span>
    <select id="tr-run" class="max-w-[16rem] text-[11px]"><option value="latest" ${TR.sel === "latest" ? "selected" : ""}>▶ acompanhar o mais recente</option>${d.runs.map((r) => `<option value="${esc(r.run)}" ${TR.sel === r.run ? "selected" : ""}>${r.state === "running" ? "● " : r.state === "error" ? "✕ " : "✓ "}${esc(hms(r.started))} · ${esc(r.title)}</option>`).join("")}</select>
    <button id="tr-close" class="text-2xl leading-none text-slate-400 hover:text-white">×</button>`;
  $("#tr-run").onchange = (e) => { TR.sel = e.target.value; TR.cursor = ""; TR.run = ""; trReset(); trPoll(); };
  $("#tr-close").onclick = () => $("#dlg-trace").close();
}
function trReset() { TR.rows.clear(); TR.els.clear(); TR.open.clear(); const f = $("#tr-feed"); if (f) f.innerHTML = ""; }
async function trPoll() {
  if (!$("#dlg-trace").open) return;
  try {
    const q = new URLSearchParams({ run: TR.sel === "latest" ? "" : TR.sel, cursor: TR.cursor });
    const d = await api(`/agents/${TR.slug}/trace?${q}`);
    const cur = d.run?.run || "";
    if (cur !== TR.run) { TR.run = cur; if (TR.cursor) { TR.cursor = ""; trReset(); return trPoll(); } trReset(); }  // run novo: recarrega inteiro
    TR.last = d; TR.cursor = d.cursor || TR.cursor;
    const feed = $("#tr-feed"), near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    for (const r of d.rows) {
      TR.rows.set(r.id, r);
      let el = TR.els.get(r.id);
      if (!el) { el = document.createElement("div"); el.className = "rounded-lg px-1.5 py-1 transition-colors"; el.dataset.rid = r.id; feed.appendChild(el); TR.els.set(r.id, el); }
      el.innerHTML = trRow(r);
      el.querySelectorAll("details").forEach((dt) => (dt.ontoggle = () => TR.open.set(+dt.dataset.rid, dt.open)));
    }
    if (!TR.rows.size) feed.innerHTML = '<p class="p-4 text-slate-500">Este agente ainda não executou nenhuma tarefa.</p>';
    trHead(); trSide();
    if (TR.follow && near && d.rows.length) feed.scrollTop = feed.scrollHeight;
  } catch (e) { /* próxima volta tenta de novo */ }
}
function openTrace(slug) {
  Object.assign(TR, { slug, sel: "latest", run: "", cursor: "", last: null, follow: true });
  trReset();
  $("#trace-root").innerHTML = `<div id="tr-head" class="flex items-center gap-3 border-b border-slate-800 p-3"></div>
    <div class="grid min-h-0 flex-1 grid-cols-1 md:grid-cols-[19rem_1fr]">
      <div id="tr-side" class="overflow-y-auto border-b border-slate-800 p-3 text-xs md:border-b-0 md:border-r"></div>
      <div id="tr-feed" class="space-y-1 overflow-y-auto p-3 text-xs leading-relaxed"></div></div>
    <div class="flex items-center gap-4 border-t border-slate-800 px-3 py-2 text-[11px] text-slate-400">
      <label class="!mb-0 flex items-center gap-1.5 !normal-case !tracking-normal"><input id="tr-follow" type="checkbox" checked> <span class="font-normal">rolar automaticamente</span></label>
      <button id="tr-expand" class="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800">expandir tudo</button><button id="tr-collapse" class="rounded border border-slate-700 px-2 py-0.5 hover:bg-slate-800">recolher tudo</button>
      <span class="ml-auto">atualiza a cada 1 s · cada tarefa guarda as últimas execuções</span></div>`;
  $("#tr-follow").onchange = (e) => (TR.follow = e.target.checked);
  const all = (v) => { TR.rows.forEach((r) => TR.open.set(r.id, v)); TR.els.forEach((el, id) => { const r = TR.rows.get(id); el.innerHTML = trRow(r); el.querySelectorAll("details").forEach((dt) => (dt.ontoggle = () => TR.open.set(+dt.dataset.rid, dt.open))); }); };
  $("#tr-expand").onclick = () => all(true); $("#tr-collapse").onclick = () => all(false);
  const dlg = $("#dlg-trace"); if (!dlg.open) dlg.showModal();
  dlg.onclose = () => { clearInterval(TR.timer); TR.timer = null; };
  clearInterval(TR.timer); TR.timer = setInterval(trPoll, 1000); trPoll();
}

renderTabs(); refresh(); setInterval(refresh, 1500);
