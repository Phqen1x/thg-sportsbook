// Panem Sportsbook — Discord Activity SPA (no-build, vanilla ES module).
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

// Small popup anchored next to whichever button triggered it — used whenever a
// leg or whole parlay is added to the slip, so the member gets feedback right
// where they're looking instead of being redirected to the Parlay tab. Built
// fresh each time and appended to <body> so it's never clipped by a scrolling/
// overflow-hidden ancestor (the nav bar, a card list, etc.).
function notifyNearButton(anchorEl, message, isError = false) {
  if (!anchorEl || !anchorEl.getBoundingClientRect) return toast(message, isError ? "error" : "success");
  const bubble = document.createElement("div");
  bubble.className = "inline-popup" + (isError ? " error" : "");
  bubble.textContent = message;
  document.body.appendChild(bubble);

  const anchorRect = anchorEl.getBoundingClientRect();
  const bubbleRect = bubble.getBoundingClientRect();
  let left = anchorRect.left + anchorRect.width / 2 - bubbleRect.width / 2;
  left = Math.max(6, Math.min(left, window.innerWidth - bubbleRect.width - 6));
  let top = anchorRect.bottom + 6;
  if (top + bubbleRect.height > window.innerHeight - 6) {
    top = anchorRect.top - bubbleRect.height - 6;
  }
  bubble.style.left = `${left}px`;
  bubble.style.top = `${top}px`;

  requestAnimationFrame(() => bubble.classList.add("show"));
  setTimeout(() => {
    bubble.classList.remove("show");
    setTimeout(() => bubble.remove(), 200);
  }, 2600);
}

// ── Auth ─────────────────────────────────────────────────────────────────────

async function authenticate() {
  const params = new URLSearchParams(location.search);
  const isEmbedded =
    location.hostname.endsWith("discordsays.com") || params.has("frame_id");

  // Dev/standalone shortcut: allow a pre-minted token via ?token= or localStorage
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
  // Keep as string — Discord snowflakes exceed Number.MAX_SAFE_INTEGER and
  // would silently lose precision if converted to a JS number.
  const guildId = SDK.guildId || null;
  const { code } = await SDK.commands.authorize({
    client_id: CFG.clientId,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify"],
  });
  const result = await api("/token", { method: "POST", body: { code, guild_id: guildId } });
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
      <div class="brand"><img src="static/panem.png" alt="" class="brand-logo"> PANEM</div>
      <nav class="tabs" id="tabs">
        ${tabs.map(([id, label]) =>
          `<a href="${location.pathname}${location.search}#${id}" data-tab="${id}">${label}</a>`).join("")}
      </nav>
      <a href="${location.pathname}${location.search}#balance" class="me">
        <span id="balance" class="chips">${fmtChips(ME?.chips)} chips</span>
        <img class="avatar" src="${esc(ME?.avatar_url || "")}" alt="">
      </a>
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
  balance: viewBalance,
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

// Market types where tribute_a/tribute_b are combined into one joint outcome
// (e.g. their scores summed) rather than pitted head-to-head — keep in sync
// with COMBINED_PAIR_MARKET_TYPES in web/app.py.
const COMBINED_PAIR_MARKET_TYPES = new Set(["COMBINED_DISTRICT_SCORE"]);

function marketSubtitle(m) {
  if (!m.tribute_a || !m.tribute_b) return esc(m.tribute_a || m.tribute_b || "");
  const joiner = COMBINED_PAIR_MARKET_TYPES.has(m.type) ? "and" : "vs";
  return `${esc(m.tribute_a)} ${joiner} ${esc(m.tribute_b)}`;
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
      ${m.status === "CLOSED" ? `<button class="btn btn-outline" data-act="m-open" data-id="${m.id}">Open</button>` : ""}
      ${m.status === "OPEN" ? `<button class="btn btn-outline" data-act="m-close" data-id="${m.id}">Close</button>` : ""}
      ${m.status === "RESOLVED" ? `<button class="btn btn-outline" data-act="m-reopen" data-id="${m.id}">Reopen</button>` : ""}
      ${m.status !== "RESOLVED" ? `<button class="btn btn-primary" data-act="m-resolve" data-id="${m.id}">Resolve</button>` : ""}
      ${m.status !== "RESOLVED" ? `<button class="btn btn-outline" data-act="m-set-odds" data-id="${m.id}" data-odds="${m.odds}">Set Odds</button>` : ""}
      ${m.odds_override ? `<button class="btn btn-outline" data-act="m-clear-override" data-id="${m.id}" title="Clear manual odds override">Unlock</button>` : ""}`;
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

// Category filter logic: returns true if market matches category key
const VICTOR_TYPES = new Set(["TRIBUTE_WINS", "DISTRICT_VICTOR", "ALLIANCE_VICTOR"]);

function matchesCat(m, cat) {
  if (!cat) return true;
  if (cat === "victor")   return VICTOR_TYPES.has(m.type);
  if (cat === "tribute")  return !VICTOR_TYPES.has(m.type) && (m.tribute_a != null || m.tribute_b != null);
  if (cat === "district") return m.type === "DISTRICT_VICTOR";
  if (cat === "alliance") return m.type === "ALLIANCE_VICTOR";
  if (cat === "props")    return !VICTOR_TYPES.has(m.type) && m.tribute_a == null && m.tribute_b == null;
  return true;
}

// ── Views: Markets (home) ──────────────────────────────────────────────────────

const CATS = [
  { key: "",         icon: "⚔️",  label: "All" },
  { key: "victor",   icon: "🏆",  label: "Victor" },
  { key: "tribute",  icon: "🗡️",  label: "Tributes" },
  { key: "district", icon: "🏰",  label: "Districts" },
  { key: "alliance", icon: "🤝",  label: "Alliances" },
  { key: "props",    icon: "🎯",  label: "Props" },
];

async function viewMarkets(view) {
  const [marketsData, tailData, bannersData] = await Promise.all([
    api("/markets?status=open"),
    api("/tail").catch(() => ({ templates: [] })),
    api("/banners").catch(() => ({ banners: [] })),
  ]);

  const allMarkets = marketsData.markets;
  const templates  = tailData.templates || [];
  const banners    = bannersData.banners || [];

  view.innerHTML = `
    ${marketsData.phase_name
      ? `<div class="phase-banner">Phase: ${esc(marketsData.phase_name)}</div>`
      : ""}

    <div class="home-search">
      <input class="home-search-input" id="mkt-search"
        placeholder="Search tributes, districts, alliances…" autocomplete="off" type="search">
    </div>

    <div class="cat-pills" id="cat-pills">
      ${CATS.map((c) => `
        <button class="cat-pill${c.key === "" ? " active" : ""}" data-cat="${esc(c.key)}">
          <span class="cat-icon">${c.icon}</span>
          <span class="cat-label">${c.label}</span>
        </button>`).join("")}
    </div>

    ${banners.length ? `
    <div class="promo-rail">
      ${banners.map((b) => `
        <div class="promo-card" style="${b.color ? `border-color:${esc(b.color)}` : ""}">
          <div class="promo-emoji">${esc(b.emoji || "🏆")}</div>
          <div class="promo-body">
            <div class="promo-title">${esc(b.title)}</div>
            ${b.subtitle ? `<div class="promo-sub">${esc(b.subtitle)}</div>` : ""}
          </div>
          ${b.cta ? `<div class="promo-cta">${esc(b.cta)}</div>` : ""}
        </div>`).join("")}
    </div>` : ""}

    <h2 class="section-title" id="mkts-heading">Open Markets</h2>
    <div class="list" id="market-list">
      ${allMarkets.length
        ? allMarkets.map((m) => marketCard(m)).join("")
        : marketsData.phase_name
          ? `<div class="empty">No open markets right now.</div>`
          : `<div class="empty">The Games haven't started yet — check back once an admin kicks things off.</div>`}
    </div>

    ${templates.length ? `
    <h2 class="section-title">Featured Parlays</h2>
    <div class="feat-rail">
      ${templates.map((t) => featuredParlayCard(t)).join("")}
    </div>` : ""}`;

  bindMemberMarketActions(view);
  bindFeaturedParlayActions(view);

  let activeCat = "";
  let searchQ   = "";

  function refilter() {
    const q = searchQ.toLowerCase();
    const filtered = allMarkets.filter((m) => {
      if (!matchesCat(m, activeCat)) return false;
      if (!q) return true;
      return (
        m.label.toLowerCase().includes(q) ||
        (m.tribute_a && m.tribute_a.toLowerCase().includes(q)) ||
        (m.tribute_b && m.tribute_b.toLowerCase().includes(q))
      );
    });
    const listEl = $("#market-list", view);
    const heading = $("#mkts-heading", view);
    if (heading) heading.textContent = `Open Markets${filtered.length !== allMarkets.length ? ` (${filtered.length})` : ""}`;
    listEl.innerHTML = filtered.length
      ? filtered.map((m) => marketCard(m)).join("")
      : `<div class="empty">No markets match.</div>`;
    bindMemberMarketActions(view);
  }

  const searchEl = $("#mkt-search", view);
  searchEl.addEventListener("input", () => { searchQ = searchEl.value.trim(); refilter(); });

  view.querySelectorAll(".cat-pill").forEach((pill) =>
    pill.addEventListener("click", () => {
      view.querySelectorAll(".cat-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      activeCat = pill.dataset.cat;
      refilter();
    }));
}

function featuredParlayCard(t) {
  return `
    <div class="feat-parlay-card card">
      <div class="feat-parlay-head">
        <span class="feat-parlay-name">${esc(t.name)}</span>
        ${t.difficulty ? `<span class="badge">${esc(t.difficulty)}</span>` : ""}
      </div>
      ${t.description ? `<div class="dim feat-parlay-desc">${esc(t.description)}</div>` : ""}
      <ul class="parlay-legs">
        ${t.legs.map((m) => `<li>${esc(m.label)} <span class="${oddsClass(m.odds)}">${fmtOdds(m.odds)}</span></li>`).join("")}
      </ul>
      <div class="feat-parlay-footer">
        <span class="${oddsClass(t.combined_odds)} feat-parlay-odds">${t.combined_odds == null ? "—" : fmtOdds(t.combined_odds)}</span>
        <button class="btn btn-primary btn-sm" data-act="tail" data-id="${t.id}">Tail this</button>
        <button class="btn btn-outline btn-sm" data-act="add-slip" data-id="${t.id}">Add to Slip</button>
      </div>
    </div>`;
}

function bindFeaturedParlayActions(view) {
  view.querySelectorAll('[data-act="tail"]').forEach((b) =>
    b.addEventListener("click", () => openWagerModal({
      title: "Tail Parlay",
      onSubmit: (wager) => api(`/tail/${b.dataset.id}`, { method: "POST", body: { wager } }),
      after: () => { location.hash = "#mybets"; },
    })));
  view.querySelectorAll('[data-act="add-slip"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/tail/${b.dataset.id}/add-to-slip`, { method: "POST" });
        notifyNearButton(b, r.message);
      } catch (e) { notifyNearButton(b, e.message, true); }
    }));
}

function bindMemberMarketActions(view) {
  view.querySelectorAll('[data-act="bet"]').forEach((b) =>
    b.addEventListener("click", () => openBetModal(Number(b.dataset.id))));
  view.querySelectorAll('[data-act="add-parlay"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/parlay/add/${b.dataset.id}`, { method: "POST" });
        notifyNearButton(b, r.message);
      } catch (e) { notifyNearButton(b, e.message, true); }
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
  const cat = location.hash.split("/")[1] || "CHIPS";
  const base = location.pathname + location.search;
  const { users, title, value_kind, categories } = await api(`/leaderboard?category=${encodeURIComponent(cat)}`);
  view.innerHTML = `
    <div class="subtabs" style="flex-wrap:wrap">
      ${categories.map((c) => `
        <a href="${base}#leaderboard/${c.value}" class="${cat === c.value ? "active" : ""}">${esc(c.label)}</a>`).join("")}
    </div>
    <h2 class="section-title">${esc(title)}</h2>
    <div class="list leaderboard">
      ${users.length ? users.map((u) => `
        <div class="card lb-row ${u.is_me ? "lb-me" : ""}">
          <span class="lb-rank">#${u.rank}</span>
          <span class="lb-name">${esc(u.username)}</span>
          <span class="lb-chips chips">${value_kind === "chips" ? fmtChips(u.value) : u.value}</span>
        </div>`).join("") : `<div class="empty">No players yet.</div>`}
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
      ${b.status === "PENDING" && b.cashout_preview != null ? `<button class="btn btn-outline btn-sm" data-act="cashout-bet" data-id="${b.id}" data-amount="${b.cashout_preview}">Cash out (${fmtChips(b.cashout_preview)})</button>` : ""}
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
      ${p.status === "PENDING" && p.cashout_preview != null ? `<button class="btn btn-outline btn-sm" data-act="cashout-parlay" data-id="${p.id}" data-amount="${p.cashout_preview}">Cash out (${fmtChips(p.cashout_preview)})</button>` : ""}
    </div>`).join("");

  view.innerHTML = `
    <h2 class="section-title">Straight Bets</h2>
    <div class="list">${straight || `<div class="empty">No straight bets yet.</div>`}</div>
    <h2 class="section-title">Parlays</h2>
    <div class="list">${parlays || `<div class="empty">No parlays yet.</div>`}</div>`;

  view.querySelectorAll('[data-act="cashout-bet"]').forEach((b) =>
    b.addEventListener("click", () => {
      if (!confirm(`Cash out this bet for ${fmtChips(b.dataset.amount)} chips?`)) return;
      doAction(`/cashout/bet/${b.dataset.id}`, "POST");
    }));
  view.querySelectorAll('[data-act="cashout-parlay"]').forEach((b) =>
    b.addEventListener("click", () => {
      if (!confirm(`Cash out this parlay for ${fmtChips(b.dataset.amount)} chips?`)) return;
      doAction(`/cashout/parlay/${b.dataset.id}`, "POST");
    }));
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
          <div class="row-buttons">
            <button class="btn btn-primary btn-sm" data-act="tail" data-id="${t.id}">Tail this</button>
            <button class="btn btn-outline btn-sm" data-act="add-slip" data-id="${t.id}">Add to Slip</button>
          </div>
        </div>`).join("") : `<div class="empty">No public parlays to tail right now.</div>`}
    </div>`;

  view.querySelectorAll('[data-act="tail"]').forEach((b) =>
    b.addEventListener("click", () => openWagerModal({
      title: "Tail Parlay",
      onSubmit: (wager) => api(`/tail/${b.dataset.id}`, { method: "POST", body: { wager } }),
      after: () => { location.hash = "#mybets"; },
    })));
  view.querySelectorAll('[data-act="add-slip"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`/tail/${b.dataset.id}/add-to-slip`, { method: "POST" });
        notifyNearButton(b, r.message);
      } catch (e) { notifyNearButton(b, e.message, true); }
    }));
}

// ── Views: Balance / profile ───────────────────────────────────────────────────

async function viewBalance(view) {
  const [meData, betsData] = await Promise.all([api("/me"), api("/my-bets")]);
  ME = { ...ME, ...meData };

  const straight = betsData.straight_bets;
  const parlays  = betsData.parlays;
  const resolved = straight.filter((b) => b.status !== "PENDING" && b.status !== "VOIDED");
  const wonCount = resolved.filter((b) => b.status === "WON").length;
  const winRate  = resolved.length ? `${((wonCount / resolved.length) * 100).toFixed(1)}%` : "—";
  const roi      = meData.roi ?? 0;

  const activity = [
    ...straight.map((b) => ({ type: "STRAIGHT", wager: b.wager, status: b.status, date: b.placed_at })),
    ...parlays.map((p)  => ({ type: "PARLAY",   wager: p.total_wager, status: p.status, date: p.placed_at })),
  ].sort((a, b) => (b.date || "").localeCompare(a.date || "")).slice(0, 15);

  view.innerHTML = `
    <h2 class="section-title">Your Balance</h2>
    <div class="balance-layout">
      <div class="card balance-chip-card">
        <div class="balance-chip-count">${fmtChips(meData.chips)}</div>
        <div class="dim">chips</div>
      </div>
      <div class="card">
        <table class="balance-stats-table">
          <tr><td class="dim">Total Wagered</td><td class="chips">${fmtChips(meData.total_wagered)}</td></tr>
          <tr><td class="dim">Total Won</td><td class="odds-pos">${fmtChips(meData.total_won)}</td></tr>
          <tr><td class="dim">ROI</td><td class="${roi >= 0 ? "odds-pos" : "odds-neg"}">${roi >= 0 ? "+" : ""}${roi}%</td></tr>
          <tr><td class="dim">Straight Bets</td><td>${straight.length}</td></tr>
          <tr><td class="dim">Parlays</td><td>${parlays.length}</td></tr>
          <tr><td class="dim">Win Rate</td><td>${winRate}</td></tr>
        </table>
      </div>
    </div>
    <h2 class="section-title">Recent Activity</h2>
    <div class="list">
      ${activity.length ? activity.map((a) => `
        <div class="card bet-row">
          <span class="badge">${esc(a.type)}</span>
          <div class="bet-main">
            <span class="dim">${fmtChips(a.wager)} chips wagered</span>
          </div>
          <span class="status status-${esc(a.status.toLowerCase())}">${esc(a.status)}</span>
        </div>`).join("") : `<div class="empty">No activity yet.</div>`}
    </div>`;
}

// ── Views: Admin (live-game ops) ───────────────────────────────────────────────

async function viewAdmin(view) {
  const sub = (location.hash.split("/")[1]) || "markets";
  const base = location.pathname + location.search;
  const status = await api("/admin/game/status").catch(() => ({ game_active: true, phase_name: null }));
  view.innerHTML = `
    ${status.game_active ? "" : `
    <div class="card admin-start-game">
      <div>
        <div class="card-label">The Games haven't started</div>
        <div class="dim">Starting opens Pre-Games markets and seeds the tailing board.</div>
      </div>
      <button class="btn btn-primary" id="admin-start-game">⚡ Start the Games</button>
    </div>`}
    <div class="subtabs">
      <a href="${base}#admin/markets"  class="${sub === "markets"  ? "active" : ""}">Markets</a>
      <a href="${base}#admin/chips"    class="${sub === "chips"    ? "active" : ""}">Chips</a>
      <a href="${base}#admin/tributes" class="${sub === "tributes" ? "active" : ""}">Tributes</a>
      <a href="${base}#admin/banners"  class="${sub === "banners"  ? "active" : ""}">Banners</a>
    </div>
    <div id="admin-body"><div class="loading-inline">Loading…</div></div>`;
  const startBtn = $("#admin-start-game", view);
  if (startBtn) {
    startBtn.addEventListener("click", () => {
      if (!confirm("Start the Games? This opens Pre-Games markets and seeds the tailing board.")) return;
      doAction("/admin/game/start", "POST");
    });
  }
  const body = $("#admin-body", view);
  if (sub === "chips")    return adminChips(body);
  if (sub === "tributes") return adminTributes(body);
  if (sub === "banners")  return adminBanners(body);
  return adminMarkets(body);
}

async function adminMarkets(body) {
  const [openData, closedData, resolvedData] = await Promise.all([
    api(`/markets?status=open`),
    api(`/markets?status=closed`),
    api(`/markets?status=resolved`),
  ]);
  const all = [...openData.markets, ...closedData.markets, ...resolvedData.markets];
  body.innerHTML = `
    <div class="admin-market-bar">
      <button class="btn btn-outline btn-sm" id="m-recalc">Recalculate All Odds</button>
      <button class="btn btn-outline btn-sm" id="m-bulk-close">Bulk Close All Open</button>
    </div>
    <div class="list">
      ${all.length ? all.map((m) => marketCard(m, { actions: "admin" })).join("") : `<div class="empty">No markets.</div>`}
    </div>`;
  $("#m-recalc", body).addEventListener("click", async () => {
    if (!confirm("Recalculate odds on all non-overridden markets?")) return;
    await doAction("/admin/markets/recalc", "POST");
  });
  $("#m-bulk-close", body).addEventListener("click", async () => {
    if (!confirm("Close all open markets?")) return;
    await doAction("/admin/markets/bulk-close", "POST");
  });
  body.querySelectorAll('[data-act="m-open"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/admin/market/${b.dataset.id}/open`, "POST")));
  body.querySelectorAll('[data-act="m-close"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/admin/market/${b.dataset.id}/close`, "POST")));
  body.querySelectorAll('[data-act="m-reopen"]').forEach((b) =>
    b.addEventListener("click", () => {
      if (!confirm("Reopen this resolved market?")) return;
      doAction(`/admin/market/${b.dataset.id}/reopen`, "POST");
    }));
  body.querySelectorAll('[data-act="m-resolve"]').forEach((b) =>
    b.addEventListener("click", () => openResolveModal(Number(b.dataset.id))));
  body.querySelectorAll('[data-act="m-set-odds"]').forEach((b) =>
    b.addEventListener("click", () => openSetOddsModal(Number(b.dataset.id), Number(b.dataset.odds))));
  body.querySelectorAll('[data-act="m-clear-override"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/admin/market/${b.dataset.id}/clear-override`, "POST")));
}

async function adminChips(body) {
  const { users } = await api("/admin/users");
  body.innerHTML = `
    <div class="card admin-form">
      <div class="card-label">GIVE / TAKE</div>
      <input id="chip-id" class="input" placeholder="Discord user ID (click row below)">
      <input id="chip-amt" class="input" type="number" min="1" placeholder="Amount">
      <div class="row-buttons">
        <button class="btn btn-primary" id="chip-give">Give</button>
        <button class="btn btn-outline" id="chip-take">Take</button>
        <button class="btn btn-outline" id="chip-set">Set Balance</button>
      </div>
    </div>
    <div class="card admin-form">
      <div class="card-label">GIVE ALL PLAYERS</div>
      <input id="chip-all-amt" class="input" type="number" min="1" placeholder="Amount per player" value="500">
      <button class="btn btn-primary" id="chip-give-all">Give to Everyone</button>
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
  const send = async (path, extraBody) => {
    const discord_id = $("#chip-id", body).value.trim();
    const amount = Number($("#chip-amt", body).value);
    if (!discord_id || !amount) return toast("Enter a user ID and amount.", "error");
    try {
      const r = await api(path, { method: "POST", body: { discord_id, amount, ...extraBody } });
      toast(r.message);
      adminChips(body);
    } catch (e) { toast(e.message, "error"); }
  };
  $("#chip-give", body).addEventListener("click", () => send("/admin/chips/give"));
  $("#chip-take", body).addEventListener("click", () => send("/admin/chips/take"));
  $("#chip-set", body).addEventListener("click", () => {
    if (!confirm("Set this exact chip balance?")) return;
    send("/admin/chips/set");
  });
  $("#chip-give-all", body).addEventListener("click", async () => {
    const amount = Number($("#chip-all-amt", body).value);
    if (!amount || amount < 1) return toast("Enter a valid amount.", "error");
    if (!confirm(`Give ${amount.toLocaleString()} chips to all ${users.length} players?`)) return;
    try {
      const r = await api("/admin/chips/give-all", { method: "POST", body: { amount } });
      toast(r.message);
      adminChips(body);
    } catch (e) { toast(e.message, "error"); }
  });
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
            <button class="btn btn-outline btn-sm" data-act="kill"   data-id="${t.id}" data-name="${esc(t.name)}">Eliminate</button>
            <button class="btn btn-primary btn-sm" data-act="victor" data-id="${t.id}" data-name="${esc(t.name)}">Victor</button>
          </div>` : ""}
        ${t.status === "DEAD" ? `
          <div class="row-buttons">
            <button class="btn btn-outline btn-sm" data-act="unkill" data-id="${t.id}" data-name="${esc(t.name)}">Unkill</button>
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
  body.querySelectorAll('[data-act="unkill"]').forEach((b) =>
    b.addEventListener("click", async () => {
      if (!confirm(`Revive ${b.dataset.name}? This will revert their kill record.`)) return;
      await doAction(`/admin/tribute/${b.dataset.id}/unkill`, "POST");
    }));
}

async function adminBanners(body) {
  const { banners } = await api("/banners").catch(() => ({ banners: [] }));
  body.innerHTML = `
    <div class="card admin-form" id="banner-form">
      <input id="b-title"    class="input" placeholder="Title (required)" maxlength="80">
      <input id="b-subtitle" class="input" placeholder="Subtitle (optional)" maxlength="120">
      <div class="row-buttons">
        <input id="b-emoji" class="input" placeholder="Emoji e.g. 🏆" style="max-width:90px" maxlength="8">
        <input id="b-cta"   class="input" placeholder="Button text e.g. Opt In" maxlength="30">
        <input id="b-color" class="input" placeholder="#hex accent (optional)" maxlength="20">
      </div>
      <button class="btn btn-primary" id="b-add">Add Banner</button>
    </div>
    <div class="list" id="banner-list">
      ${banners.length
        ? banners.map((b) => `
          <div class="card market-card">
            <div class="market-main">
              <div class="market-label">${esc(b.emoji || "")} ${esc(b.title)}</div>
              ${b.subtitle ? `<div class="market-sub">${esc(b.subtitle)}</div>` : ""}
              ${b.cta ? `<div class="dim">${esc(b.cta)}</div>` : ""}
            </div>
            <button class="btn btn-outline btn-sm" data-act="del-banner" data-id="${esc(b.id)}">Remove</button>
          </div>`).join("")
        : `<div class="empty">No banners yet.</div>`}
    </div>`;

  $("#b-add", body).addEventListener("click", async () => {
    const title    = $("#b-title", body).value.trim();
    const subtitle = $("#b-subtitle", body).value.trim();
    const emoji    = $("#b-emoji", body).value.trim() || "🏆";
    const cta      = $("#b-cta", body).value.trim();
    const color    = $("#b-color", body).value.trim();
    if (!title) return toast("Title is required.", "error");
    try {
      const r = await api("/admin/banners/add", { method: "POST", body: { title, subtitle, emoji, cta, color } });
      toast(r.message);
      adminBanners(body);
    } catch (e) { toast(e.message, "error"); }
  });

  body.querySelectorAll('[data-act="del-banner"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/banners/${btn.dataset.id}`, { method: "DELETE" });
        toast(r.message);
        adminBanners(body);
      } catch (e) { toast(e.message, "error"); }
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

function openSetOddsModal(marketId, currentOdds) {
  const overlay = modal(`
    <h3>Set Manual Odds</h3>
    <div class="dim">Locks odds — calculator will not override until cleared.</div>
    <input id="so-odds" class="input" type="number" value="${currentOdds}" placeholder="e.g. -110 or +200">
    <div class="row-buttons">
      <button class="btn btn-primary" id="so-go">Set Odds</button>
      <button class="btn btn-outline" id="so-cancel">Cancel</button>
    </div>`);
  $("#so-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#so-go", overlay).addEventListener("click", async () => {
    const odds = Number($("#so-odds", overlay).value);
    if (!odds) return toast("Enter valid odds.", "error");
    try {
      const r = await api(`/admin/market/${marketId}/set-odds`, { method: "POST", body: { odds } });
      toast(r.message);
      overlay.remove();
      route();
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
    <input id="k-cause"  class="input" placeholder="Death cause" value="Another Tribute">
    <input id="k-killer" class="input" type="number" placeholder="Killed by (tribute ID, optional)">
    <input id="k-place"  class="input" type="number" placeholder="Final placement (optional)">
    <div class="row-buttons">
      <button class="btn btn-lost"    id="k-go">Eliminate</button>
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
    document.getElementById("app").innerHTML = `
      <div class="fatal">
        <div class="loading-crest">⚔</div>
        <p>${esc(e.message)}</p>
        <button class="btn btn-outline" onclick="location.reload()">Try Again</button>
      </div>`;
  }
})();
