// Capitol Sportsbook — Discord Activity SPA (no-build, vanilla ES module).
// Handles the Embedded App SDK auth handshake, then drives a small hash router
// over the JSON API in web/routes/activity.py. Identity + admin rights are
// established server-side; this client only carries the signed bearer token.

import { DiscordSDK } from "./discord-sdk.js";

const CFG = window.__ACTIVITY__ || { proxy: "", clientId: "" };
const API = `${CFG.proxy}/api/activity`;

let TOKEN = null;   // signed activity token (Authorization: Bearer)
let ME = null;      // { discord_id, username, avatar_url, is_admin, chips, roi }
let SDK = null;

// ── Tiny utils ───────────────────────────────────────────────────────────────

const $ = (sel, root = document) => root.querySelector(sel);
const fmtChips = (n) => Number(n ?? 0).toLocaleString("en-US");
const fmtOdds = (n) => (n == null ? "—" : n >= 0 ? `+${n}` : `${n}`);
const oddsClass = (n) => (n == null ? "" : n >= 0 ? "odds-pos" : "odds-neg");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: {
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

let _toastTimer = null;
function toast(message, type = "success") {
  let t = $("#toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.className = `toast toast-${type} show`;
  t.textContent = message;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove("show"), 3500);
}

// ── Auth ─────────────────────────────────────────────────────────────────────

async function authenticate() {
  const params = new URLSearchParams(location.search);
  const isEmbedded =
    location.hostname.endsWith("discordsays.com") || params.has("frame_id");

  // Dev/standalone shortcut: allow a pre-minted token via ?token= or localStorage
  // so the UI/API can be exercised in a normal browser tab.
  const devToken = params.get("token") || localStorage.getItem("sb_dev_token");
  if (!isEmbedded && devToken) {
    TOKEN = devToken;
    ME = await api("/me");
    return;
  }
  if (!isEmbedded) {
    throw new Error("Launch this from Discord → Activities to sign in.");
  }

  SDK = new DiscordSDK(CFG.clientId);
  await SDK.ready();
  const { code } = await SDK.commands.authorize({
    client_id: CFG.clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify", "guilds", "guilds.members.read"],
  });
  const result = await api("/token", { method: "POST", body: { code } });
  TOKEN = result.token;
  await SDK.commands.authenticate({ access_token: result.access_token });
  ME = { ...result.user, chips: 0 };
  await refreshMe();
}

async function refreshMe() {
  try {
    const m = await api("/me");
    ME = { ...ME, ...m };
    const bal = $("#balance");
    if (bal) bal.textContent = `${fmtChips(ME.chips)} chips`;
  } catch (_) { /* ignore */ }
}

// ── Shell / router ─────────────────────────────────────────────────────────────

const TABS = [
  ["markets", "Markets"],
  ["tributes", "Tributes"],
  ["leaderboard", "Leaderboard"],
  ["mybets", "My Bets"],
  ["parlay", "Parlay"],
  ["tail", "Tail"],
];

function renderShell() {
  const tabs = [...TABS];
  if (ME?.is_admin) tabs.push(["admin", "Admin"]);
  document.getElementById("app").innerHTML = `
    <header class="topbar">
      <div class="brand">⚔ CAPITOL</div>
      <nav class="tabs" id="tabs">
        ${tabs.map(([id, label]) =>
          `<a href="${location.pathname}${location.search}#${id}" data-tab="${id}">${label}</a>`).join("")}
      </nav>
      <div class="me">
        <span id="balance" class="chips">${fmtChips(ME?.chips)} chips</span>
        <img class="avatar" src="${esc(ME?.avatar_url || "")}" alt="">
      </div>
    </header>
    <main id="view" class="view"></main>`;
  window.addEventListener("hashchange", route);
}

const VIEWS = {
  markets: viewMarkets,
  tributes: viewTributes,
  leaderboard: viewLeaderboard,
  mybets: viewMyBets,
  parlay: viewParlay,
  tail: viewTail,
  admin: viewAdmin,
};

async function route() {
  let tab = (location.hash || "#markets").slice(1).split("/")[0];
  if (!VIEWS[tab] || (tab === "admin" && !ME?.is_admin)) tab = "markets";
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.dataset.tab === tab));
  const view = $("#view");
  view.innerHTML = `<div class="loading-inline">Loading…</div>`;
  try {
    await VIEWS[tab](view);
  } catch (e) {
    view.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

// ── Market rendering helpers ───────────────────────────────────────────────────

function marketSubtitle(m) {
  const bits = [];
  if (m.tribute_a) bits.push(esc(m.tribute_a));
  if (m.tribute_b) bits.push(esc(m.tribute_b));
  return bits.join(" vs ");
}

function marketCard(m, { actions = "member" } = {}) {
  const sub = marketSubtitle(m);
  let buttons = "";
  if (actions === "member" && m.status === "OPEN") {
    buttons = `
      <button class="btn btn-primary" data-act="bet" data-id="${m.id}">Bet</button>
      <button class="btn btn-outline" data-act="add-parlay" data-id="${m.id}">+ Parlay</button>`;
  } else if (actions === "admin") {
    buttons = `
      ${m.status !== "OPEN" ? `<button class="btn btn-outline" data-act="m-open" data-id="${m.id}">Open</button>` : ""}
      ${m.status === "OPEN" ? `<button class="btn btn-outline" data-act="m-close" data-id="${m.id}">Close</button>` : ""}
      ${m.status !== "RESOLVED" ? `<button class="btn btn-primary" data-act="m-resolve" data-id="${m.id}">Resolve</button>` : ""}`;
  }
  return `
    <div class="card market-card">
      <div class="market-main">
        <div class="market-label">${esc(m.label)}</div>
        ${sub ? `<div class="market-sub">${sub}</div>` : ""}
        <div class="market-meta">
          <span class="status status-${esc(m.status.toLowerCase())}">${esc(m.status)}</span>
          ${m.bet_count ? `<span class="dim">· ${m.bet_count} bets</span>` : ""}
        </div>
      </div>
      <div class="market-odds ${oddsClass(m.odds)}">${fmtOdds(m.odds)}</div>
      <div class="market-actions">${buttons}</div>
    </div>`;
}

// ── Views: Markets ─────────────────────────────────────────────────────────────

async function viewMarkets(view) {
  const data = await api(`/markets?status=open`);
  const list = data.markets;
  view.innerHTML = `
    ${data.phase_name ? `<div class="phase-banner">Phase: ${esc(data.phase_name)}</div>` : ""}
    <div class="list" id="market-list">
      ${list.length ? list.map((m) => marketCard(m)).join("") : `<div class="empty">No open markets right now.</div>`}
    </div>`;
  bindMemberMarketActions(view);
}

function bindMemberMarketActions(view) {
  view.querySelectorAll('[data-act="bet"]').forEach((b) =>
    b.addEventListener("click", () => openBetModal(Number(b.dataset.id))));
  view.querySelectorAll('[data-act="add-parlay"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/parlay/add/${b.dataset.id}`, { method: "POST" });
        toast(r.message);
      } catch (e) { toast(e.message, "error"); }
    }));
}

// ── Views: Tributes ────────────────────────────────────────────────────────────

async function viewTributes(view) {
  const { tributes } = await api("/tributes");
  view.innerHTML = `
    <div class="grid">
      ${tributes.map((t) => `
        <div class="card tribute-card status-edge-${esc(t.status.toLowerCase())}">
          <div class="tribute-head">
            <span class="district">D${t.district}</span>
            <span class="status status-${esc(t.status.toLowerCase())}">${esc(t.status)}</span>
          </div>
          <div class="tribute-name">${esc(t.name)} <span class="dim">${esc(t.gender)}</span></div>
          <div class="tribute-stats dim">
            ${t.training_score != null ? `Training ${t.training_score}` : ""}
            ${t.kills ? `· ${t.kills} kills` : ""}
            ${t.alliance ? `· ${esc(t.alliance)}` : ""}
          </div>
          ${t.win_market_id && t.status === "ALIVE" ? `
            <div class="tribute-bet">
              <span class="${oddsClass(t.win_odds)}">${fmtOdds(t.win_odds)} to win</span>
              <button class="btn btn-primary btn-sm" data-act="bet" data-id="${t.win_market_id}">Bet</button>
            </div>` : ""}
        </div>`).join("")}
    </div>`;
  bindMemberMarketActions(view);
}

// ── Views: Leaderboard ─────────────────────────────────────────────────────────

async function viewLeaderboard(view) {
  const { users } = await api("/leaderboard");
  view.innerHTML = `
    <div class="list leaderboard">
      ${users.map((u) => `
        <div class="card lb-row ${u.is_me ? "lb-me" : ""}">
          <span class="lb-rank">#${u.rank}</span>
          <span class="lb-name">${esc(u.username)}</span>
          <span class="lb-chips chips">${fmtChips(u.chips)}</span>
        </div>`).join("")}
    </div>`;
}

// ── Views: My Bets ─────────────────────────────────────────────────────────────

async function viewMyBets(view) {
  const data = await api("/my-bets");
  const M = data.markets;
  const mlabel = (id) => (M[id] ? M[id].label : `Market #${id}`);

  const straight = data.straight_bets.map((b) => `
    <div class="card bet-row">
      <div class="bet-main">
        <div class="bet-label">${esc(mlabel(b.market_id))}</div>
        <div class="dim">Wager ${fmtChips(b.wager)} @ ${fmtOdds(b.odds_at_placement)} · win ${fmtChips(b.payout_if_win)}</div>
      </div>
      <span class="status status-${esc(b.status.toLowerCase())}">${esc(b.status)}</span>
      ${b.status === "PENDING" ? `<button class="btn btn-outline btn-sm" data-act="cashout-bet" data-id="${b.id}">Cash out</button>` : ""}
    </div>`).join("");

  const parlays = data.parlays.map((p) => `
    <div class="card parlay-row">
      <div class="parlay-head">
        <span>Parlay · ${p.legs.length} legs</span>
        <span class="status status-${esc(p.status.toLowerCase())}">${esc(p.status)}</span>
      </div>
      <div class="dim">Wager ${fmtChips(p.total_wager)} · payout ${fmtChips(p.total_payout)}</div>
      <ul class="parlay-legs">
        ${p.legs.map((l) => `<li><span class="leg-status status-${esc(l.status.toLowerCase())}">${esc(l.status)}</span> ${esc(mlabel(l.market_id))}</li>`).join("")}
      </ul>
      ${p.status === "PENDING" ? `<button class="btn btn-outline btn-sm" data-act="cashout-parlay" data-id="${p.id}">Cash out</button>` : ""}
    </div>`).join("");

  view.innerHTML = `
    <h2 class="section-title">Straight Bets</h2>
    <div class="list">${straight || `<div class="empty">No straight bets yet.</div>`}</div>
    <h2 class="section-title">Parlays</h2>
    <div class="list">${parlays || `<div class="empty">No parlays yet.</div>`}</div>`;

  view.querySelectorAll('[data-act="cashout-bet"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/cashout/bet/${b.dataset.id}`, "POST")));
  view.querySelectorAll('[data-act="cashout-parlay"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/cashout/parlay/${b.dataset.id}`, "POST")));
}

// ── Views: Parlay slip ─────────────────────────────────────────────────────────

async function viewParlay(view) {
  const data = await api("/parlay");
  const legs = data.legs.filter((l) => l.market);
  view.innerHTML = `
    <div class="parlay-builder">
      <div class="parlay-summary card">
        <div><span class="dim">Legs</span> ${legs.length} / ${data.max_legs}</div>
        <div><span class="dim">Combined</span> <span class="${oddsClass(data.combined_odds)}">${data.combined_odds == null ? "—" : fmtOdds(data.combined_odds)}</span></div>
      </div>
      <div class="list">
        ${legs.length ? legs.map((l) => `
          <div class="card market-card">
            <div class="market-main">
              <div class="market-label">${esc(l.market.label)}</div>
              <div class="market-meta"><span class="${oddsClass(l.market.odds)}">${fmtOdds(l.market.odds)}</span></div>
            </div>
            <button class="btn btn-outline btn-sm" data-act="remove-leg" data-id="${l.leg_id}">Remove</button>
          </div>`).join("") : `<div class="empty">Add markets from the Markets tab to build a parlay.</div>`}
      </div>
      ${legs.length >= 2 ? `
        <div class="card parlay-submit">
          <input id="parlay-wager" type="number" min="1" placeholder="Wager (chips)" class="input">
          <label class="checkbox"><input type="checkbox" id="parlay-public"> List on tail board</label>
          <div class="row-buttons">
            <button class="btn btn-primary" id="parlay-go">Submit Parlay</button>
            <button class="btn btn-outline" id="parlay-clear">Clear</button>
          </div>
        </div>` : (legs.length ? `<button class="btn btn-outline" id="parlay-clear">Clear slip</button>` : "")}
    </div>`;

  view.querySelectorAll('[data-act="remove-leg"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/parlay/remove/${b.dataset.id}`, "POST")));
  const clear = $("#parlay-clear", view);
  if (clear) clear.addEventListener("click", () => doAction("/parlay/clear", "POST"));
  const go = $("#parlay-go", view);
  if (go) go.addEventListener("click", async () => {
    const wager = Number($("#parlay-wager", view).value);
    const is_public = $("#parlay-public", view).checked;
    if (!wager || wager < 1) return toast("Enter a wager of at least 1 chip.", "error");
    try {
      const r = await api("/parlay/submit", { method: "POST", body: { wager, is_public } });
      toast(r.message);
      await refreshMe();
      location.hash = "#mybets";
    } catch (e) { toast(e.message, "error"); }
  });
}

// ── Views: Tail ────────────────────────────────────────────────────────────────

async function viewTail(view) {
  const { templates } = await api("/tail");
  view.innerHTML = `
    <div class="list">
      ${templates.length ? templates.map((t) => `
        <div class="card tail-card">
          <div class="tail-head">
            <span class="tail-name">${esc(t.name)}</span>
            ${t.difficulty ? `<span class="badge">${esc(t.difficulty)}</span>` : ""}
            <span class="${oddsClass(t.combined_odds)}">${t.combined_odds == null ? "—" : fmtOdds(t.combined_odds)}</span>
          </div>
          ${t.description ? `<div class="dim">${esc(t.description)}</div>` : ""}
          <ul class="parlay-legs">
            ${t.legs.map((m) => `<li>${esc(m.label)} <span class="${oddsClass(m.odds)}">${fmtOdds(m.odds)}</span></li>`).join("")}
          </ul>
          <button class="btn btn-primary btn-sm" data-act="tail" data-id="${t.id}">Tail this</button>
        </div>`).join("") : `<div class="empty">No public parlays to tail right now.</div>`}
    </div>`;

  view.querySelectorAll('[data-act="tail"]').forEach((b) =>
    b.addEventListener("click", () => openWagerModal({
      title: "Tail Parlay",
      onSubmit: (wager) => api(`/tail/${b.dataset.id}`, { method: "POST", body: { wager } }),
      after: () => { location.hash = "#mybets"; },
    })));
}

// ── Views: Admin (live-game ops) ───────────────────────────────────────────────

async function viewAdmin(view) {
  const sub = (location.hash.split("/")[1]) || "markets";
  view.innerHTML = `
    <div class="subtabs">
      <a href="#admin/markets" class="${sub === "markets" ? "active" : ""}">Markets</a>
      <a href="#admin/chips" class="${sub === "chips" ? "active" : ""}">Chips</a>
      <a href="#admin/tributes" class="${sub === "tributes" ? "active" : ""}">Tributes</a>
    </div>
    <div id="admin-body"><div class="loading-inline">Loading…</div></div>`;
  const body = $("#admin-body", view);
  if (sub === "chips") return adminChips(body);
  if (sub === "tributes") return adminTributes(body);
  return adminMarkets(body);
}

async function adminMarkets(body) {
  const data = await api(`/markets?status=open`);
  const closed = await api(`/markets?status=closed`);
  const all = [...data.markets, ...closed.markets];
  body.innerHTML = `<div class="list">
    ${all.length ? all.map((m) => marketCard(m, { actions: "admin" })).join("") : `<div class="empty">No markets.</div>`}
  </div>`;
  body.querySelectorAll('[data-act="m-open"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/admin/market/${b.dataset.id}/open`, "POST")));
  body.querySelectorAll('[data-act="m-close"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/admin/market/${b.dataset.id}/close`, "POST")));
  body.querySelectorAll('[data-act="m-resolve"]').forEach((b) =>
    b.addEventListener("click", () => openResolveModal(Number(b.dataset.id))));
}

async function adminChips(body) {
  const { users } = await api("/admin/users");
  body.innerHTML = `
    <div class="card admin-form">
      <input id="chip-id" class="input" placeholder="Discord user ID">
      <input id="chip-amt" class="input" type="number" min="1" placeholder="Amount">
      <div class="row-buttons">
        <button class="btn btn-primary" id="chip-give">Give</button>
        <button class="btn btn-outline" id="chip-take">Take</button>
      </div>
    </div>
    <div class="list">
      ${users.map((u) => `
        <div class="card lb-row" data-uid="${u.discord_id}">
          <span class="lb-name">${esc(u.username)}</span>
          <span class="dim">${u.discord_id}</span>
          <span class="lb-chips chips">${fmtChips(u.chips)}</span>
        </div>`).join("")}
    </div>`;
  body.querySelectorAll(".lb-row").forEach((r) =>
    r.addEventListener("click", () => { $("#chip-id", body).value = r.dataset.uid; }));
  const send = async (path) => {
    const discord_id = $("#chip-id", body).value.trim();
    const amount = Number($("#chip-amt", body).value);
    if (!discord_id || !amount) return toast("Enter a user ID and amount.", "error");
    try {
      const r = await api(path, { method: "POST", body: { discord_id, amount } });
      toast(r.message);
      adminChips(body);
    } catch (e) { toast(e.message, "error"); }
  };
  $("#chip-give", body).addEventListener("click", () => send("/admin/chips/give"));
  $("#chip-take", body).addEventListener("click", () => send("/admin/chips/take"));
}

async function adminTributes(body) {
  const { tributes } = await api("/tributes");
  body.innerHTML = `<div class="grid">
    ${tributes.map((t) => `
      <div class="card tribute-card status-edge-${esc(t.status.toLowerCase())}">
        <div class="tribute-head">
          <span class="district">D${t.district}</span>
          <span class="status status-${esc(t.status.toLowerCase())}">${esc(t.status)}</span>
        </div>
        <div class="tribute-name">${esc(t.name)} <span class="dim">${esc(t.gender)}</span></div>
        ${t.status === "ALIVE" ? `
          <div class="row-buttons">
            <button class="btn btn-outline btn-sm" data-act="kill" data-id="${t.id}" data-name="${esc(t.name)}">Eliminate</button>
            <button class="btn btn-primary btn-sm" data-act="victor" data-id="${t.id}" data-name="${esc(t.name)}">Victor</button>
          </div>` : ""}
      </div>`).join("")}
  </div>`;
  body.querySelectorAll('[data-act="kill"]').forEach((b) =>
    b.addEventListener("click", () => openKillModal(Number(b.dataset.id), b.dataset.name)));
  body.querySelectorAll('[data-act="victor"]').forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Crown ${b.dataset.name} as Victor?`)) return;
      await doAction(`/admin/tribute/${b.dataset.id}/victor`, "POST");
    }));
}

// ── Generic helpers / modals ───────────────────────────────────────────────────

async function doAction(path, method = "POST", body) {
  try {
    const r = await api(path, { method, body });
    if (r.message) toast(r.message);
    await refreshMe();
    route();
  } catch (e) { toast(e.message, "error"); }
}

function modal(innerHtml) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `<div class="modal">${innerHtml}</div>`;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  return overlay;
}

async function openBetModal(marketId) {
  // Pull current odds from the open markets list (cheap + already cached server-side).
  const data = await api(`/markets?status=open`);
  const m = data.markets.find((x) => x.id === marketId);
  if (!m) return toast("Market is no longer open.", "error");
  const overlay = modal(`
    <h3>${esc(m.label)}</h3>
    <div class="dim">Odds <span class="${oddsClass(m.odds)}">${fmtOdds(m.odds)}</span> · Balance ${fmtChips(ME.chips)}</div>
    <input id="bet-wager" class="input" type="number" min="1" max="${ME.chips}" placeholder="Wager (chips)">
    <div class="modal-payout dim" id="bet-payout"></div>
    <div class="row-buttons">
      <button class="btn btn-primary" id="bet-go">Place Bet</button>
      <button class="btn btn-outline" id="bet-cancel">Cancel</button>
    </div>`);
  const wagerEl = $("#bet-wager", overlay);
  const dec = m.odds >= 0 ? m.odds / 100 + 1 : 100 / Math.abs(m.odds) + 1;
  wagerEl.addEventListener("input", () => {
    const w = Number(wagerEl.value) || 0;
    $("#bet-payout", overlay).textContent = w ? `Win ${fmtChips(Math.max(w, Math.round(w * dec)))} chips` : "";
  });
  $("#bet-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#bet-go", overlay).addEventListener("click", async () => {
    const wager = Number(wagerEl.value);
    if (!wager || wager < 1) return toast("Enter a wager of at least 1 chip.", "error");
    try {
      const r = await api("/bet", { method: "POST", body: { market_id: marketId, wager } });
      toast(r.message);
      overlay.remove();
      await refreshMe();
    } catch (e) { toast(e.message, "error"); }
  });
}

function openWagerModal({ title, onSubmit, after }) {
  const overlay = modal(`
    <h3>${esc(title)}</h3>
    <div class="dim">Balance ${fmtChips(ME.chips)}</div>
    <input id="w-wager" class="input" type="number" min="1" placeholder="Wager (chips)">
    <div class="row-buttons">
      <button class="btn btn-primary" id="w-go">Confirm</button>
      <button class="btn btn-outline" id="w-cancel">Cancel</button>
    </div>`);
  $("#w-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#w-go", overlay).addEventListener("click", async () => {
    const wager = Number($("#w-wager", overlay).value);
    if (!wager || wager < 1) return toast("Enter a wager of at least 1 chip.", "error");
    try {
      const r = await onSubmit(wager);
      toast(r.message || "Done.");
      overlay.remove();
      await refreshMe();
      if (after) after();
    } catch (e) { toast(e.message, "error"); }
  });
}

function openResolveModal(marketId) {
  const overlay = modal(`
    <h3>Resolve Market</h3>
    <div class="dim">Choose the outcome. Bets settle immediately.</div>
    <div class="row-buttons resolve-buttons">
      <button class="btn btn-won" data-r="true">WON</button>
      <button class="btn btn-lost" data-r="false">LOST</button>
      <button class="btn btn-outline" data-r="void">VOID</button>
    </div>
    <button class="btn btn-outline" id="r-cancel">Cancel</button>`);
  $("#r-cancel", overlay).addEventListener("click", () => overlay.remove());
  overlay.querySelectorAll("[data-r]").forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/market/${marketId}/resolve`, { method: "POST", body: { result: b.dataset.r } });
        toast(r.message);
        overlay.remove();
        route();
      } catch (e) { toast(e.message, "error"); }
    }));
}

function openKillModal(tributeId, name) {
  const overlay = modal(`
    <h3>Eliminate ${esc(name)}</h3>
    <input id="k-cause" class="input" placeholder="Death cause" value="Another Tribute">
    <input id="k-killer" class="input" type="number" placeholder="Killed by (tribute ID, optional)">
    <input id="k-place" class="input" type="number" placeholder="Final placement (optional)">
    <div class="row-buttons">
      <button class="btn btn-lost" id="k-go">Eliminate</button>
      <button class="btn btn-outline" id="k-cancel">Cancel</button>
    </div>`);
  $("#k-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#k-go", overlay).addEventListener("click", async () => {
    const body = {
      death_cause: $("#k-cause", overlay).value || "Another Tribute",
      killed_by_id: $("#k-killer", overlay).value || "",
      placement: Number($("#k-place", overlay).value) || 0,
    };
    try {
      const r = await api(`/admin/tribute/${tributeId}/kill`, { method: "POST", body });
      toast(r.message);
      overlay.remove();
      route();
    } catch (e) { toast(e.message, "error"); }
  });
}

// ── Boot ───────────────────────────────────────────────────────────────────────

(async function main() {
  try {
    await authenticate();
    renderShell();
    if (!location.hash) location.hash = "#markets";
    await route();
  } catch (e) {
    document.getElementById("app").innerHTML =
      `<div class="fatal"><div class="loading-crest">⚔</div><p>${esc(e.message)}</p></div>`;
  }
})();
