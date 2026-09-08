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

// Sort choice for the admin Markets tab, kept outside adminMarkets() so it
// survives the full re-render that doAction() triggers after every action
// (open/close/resolve/etc) — otherwise closing a market would silently
// snap the list back to "Default" order.
let adminMarketsSort = "default";

// ── Tiny utils ───────────────────────────────────────────────────────────────

const $ = (sel, root = document) => root.querySelector(sel);
const fmtChips = (n) => Number(n ?? 0).toLocaleString("en-US");
const fmtOdds = (n) => (n == null ? "—" : n >= 0 ? `+${n}` : `${n}`);
const oddsClass = (n) => (n == null ? "" : n >= 0 ? "odds-pos" : "odds-neg");
const decFromOdds = (odds) => (odds >= 0 ? odds / 100 + 1 : 100 / Math.abs(odds) + 1);
const payoutForWager = (wager, odds) => Math.max(wager, Math.round(wager * decFromOdds(odds)));
// Live payout caps come from ME (see /me in web/routes/activity.py) so previews
// track admin changes without a page reload; fall back to a generous default
// (never used to actually reject a bet — the server is the source of truth)
// if ME hasn't loaded yet.
const singlePayoutCap = () => ME?.single_payout_cap ?? 10_000_000;
const parlayPayoutCap = () => ME?.parlay_payout_cap ?? 10_000_000;
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

function tabLink([id, label], extraClass = "") {
  return `<a href="${location.pathname}${location.search}#${id}" data-tab="${id}" class="${extraClass}">${label}</a>`;
}

function renderShell() {
  const isAdmin = !!ME?.is_admin;
  const tabs = [...TABS];
  if (isAdmin) tabs.push(["admin", "Admin"]);
  document.getElementById("app").innerHTML = `
    <header class="topbar">
      <div class="brand"><img src="static/panem.png" alt="" class="brand-logo"> PANEM</div>
      <nav class="tabs" id="tabs">
        ${tabs.map((t) => tabLink(t)).join("")}
      </nav>
      <a href="${location.pathname}${location.search}#balance" class="me">
        <span id="balance" class="chips">${fmtChips(ME?.chips)} chips</span>
        <img class="avatar" src="${esc(ME?.avatar_url || "")}" alt="">
      </a>
    </header>
    <nav class="tabs-grid" id="tabs-grid">
      ${TABS.map((t) => tabLink(t)).join("")}
      ${isAdmin ? tabLink(["admin", "Admin"], "tabs-grid-admin") : ""}
    </nav>
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
  document.querySelectorAll("[data-tab]").forEach((a) =>
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

const MARKETS_PAGE_SIZE = 24;

function sortMarketList(list, sortBy) {
  const arr = [...list];
  if (sortBy === "name" || sortBy === "default") arr.sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true }));
  else if (sortBy === "odds-fav") arr.sort((a, b) => (a.odds ?? Infinity) - (b.odds ?? Infinity));
  else if (sortBy === "odds-long") arr.sort((a, b) => (b.odds ?? -Infinity) - (a.odds ?? -Infinity));
  else if (sortBy === "popular") arr.sort((a, b) => (b.bet_count ?? 0) - (a.bet_count ?? 0));
  return arr;
}

const SORT_OPTIONS_HTML = `
  <option value="default">Default</option>
  <option value="name">Name (A–Z)</option>
  <option value="odds-fav">Odds: Favorites First</option>
  <option value="odds-long">Odds: Longshots First</option>
  <option value="popular">Most Bets</option>`;

async function viewMarkets(view) {
  const [marketsData, tailData, bannersData, tributesData] = await Promise.all([
    api("/markets?status=open"),
    api("/tail").catch(() => ({ templates: [] })),
    api("/banners").catch(() => ({ banners: [] })),
    api("/tributes").catch(() => ({ tributes: [] })),
  ]);

  const allMarkets = marketsData.markets;
  const templates  = tailData.templates || [];
  const banners    = bannersData.banners || [];
  const tributes   = [...(tributesData.tributes || [])]
    .sort((a, b) => a.district - b.district || a.name.localeCompare(b.name));

  view.innerHTML = `
    <div class="filter-sort-bar">
      <div class="cat-pills" id="cat-pills">
        ${CATS.map((c) => `
          <button class="cat-pill${c.key === "" ? " active" : ""}" data-cat="${esc(c.key)}">
            <span class="cat-icon">${c.icon}</span>
            <span class="cat-label">${c.label}</span>
          </button>`).join("")}
      </div>
      <div class="tribute-filter" id="tribute-filter">
        <button type="button" class="btn btn-outline btn-sm tribute-filter-btn" id="tribute-filter-btn">
          Tributes <span class="tribute-filter-count" id="tribute-filter-count"></span> ▾
        </button>
        <div class="tribute-filter-menu" id="tribute-filter-menu" hidden>
          <div class="tribute-filter-menu-actions">
            <button type="button" class="link-btn" id="tribute-filter-clear">Clear</button>
          </div>
          <div class="tribute-filter-list" id="tribute-filter-list">
            ${tributes.map((t) => `
              <label class="tribute-filter-item">
                <input type="checkbox" value="${t.id}">
                <span>D${t.district} · ${esc(t.name)}</span>
              </label>`).join("")}
          </div>
        </div>
      </div>
      <div class="sort-row">
        <label class="sort-label" for="mkt-sort">Sort</label>
        <select class="input sort-select" id="mkt-sort">${SORT_OPTIONS_HTML}</select>
      </div>
    </div>

    ${marketsData.phase_name
      ? `<div class="phase-banner">Phase: ${esc(marketsData.phase_name)}</div>`
      : ""}

    <div class="home-search">
      <input class="home-search-input" id="mkt-search"
        placeholder="Search tributes, districts, alliances…" autocomplete="off" type="search">
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
        ? ""
        : marketsData.phase_name
          ? `<div class="empty">No open markets right now.</div>`
          : `<div class="empty">The Games haven't started yet — check back once an admin kicks things off.</div>`}
    </div>
    <div class="pagination" id="market-pagination"></div>

    ${templates.length ? `
    <h2 class="section-title">Featured Parlays</h2>
    <div class="feat-rail">
      ${templates.map((t) => featuredParlayCard(t)).join("")}
    </div>` : ""}`;

  bindMemberMarketActions(view);
  bindFeaturedParlayActions(view);

  let activeCat  = "";
  let searchQ    = "";
  let sortBy     = "default";
  let page       = 1;
  const selectedTributeIds = new Set();

  function matchesTributes(m) {
    if (!selectedTributeIds.size) return true;
    return selectedTributeIds.has(String(m.tribute_a_id)) || selectedTributeIds.has(String(m.tribute_b_id));
  }

  function computeFiltered() {
    const q = searchQ.toLowerCase();
    return sortMarketList(allMarkets.filter((m) => {
      if (!matchesCat(m, activeCat)) return false;
      if (!matchesTributes(m)) return false;
      if (!q) return true;
      return (
        m.label.toLowerCase().includes(q) ||
        (m.tribute_a && m.tribute_a.toLowerCase().includes(q)) ||
        (m.tribute_b && m.tribute_b.toLowerCase().includes(q))
      );
    }), sortBy);
  }

  // Re-renders the current page from the current filter/sort/search state.
  // Filtering/sorting always runs over the full market set first, so search
  // results and filtered/sorted lists page exactly like the unfiltered list.
  function renderMarketsPage() {
    if (!allMarkets.length) return; // static empty-state markup already in place
    const filtered = computeFiltered();
    const totalPages = Math.max(1, Math.ceil(filtered.length / MARKETS_PAGE_SIZE));
    page = Math.min(Math.max(page, 1), totalPages);
    const start = (page - 1) * MARKETS_PAGE_SIZE;
    const pageItems = filtered.slice(start, start + MARKETS_PAGE_SIZE);

    const listEl = $("#market-list", view);
    const heading = $("#mkts-heading", view);
    if (heading) heading.textContent = `Open Markets${filtered.length !== allMarkets.length ? ` (${filtered.length})` : ""}`;
    listEl.innerHTML = pageItems.length
      ? pageItems.map((m) => marketCard(m)).join("")
      : `<div class="empty">No markets match.</div>`;
    bindMemberMarketActions(view);
    renderPagination(totalPages);
  }

  function renderPagination(totalPages) {
    const el = $("#market-pagination", view);
    if (!el) return;
    if (totalPages <= 1) { el.innerHTML = ""; return; }
    el.innerHTML = `
      <button type="button" class="btn btn-outline btn-sm" id="mkt-prev"${page <= 1 ? " disabled" : ""}>Prev</button>
      <span class="pagination-info">Page
        <input type="number" class="pagination-page-input" id="mkt-page-input" min="1" max="${totalPages}" value="${page}">
        of ${totalPages}
      </span>
      <button type="button" class="btn btn-outline btn-sm" id="mkt-next"${page >= totalPages ? " disabled" : ""}>Next</button>`;
    $("#mkt-prev", el).addEventListener("click", () => { page--; renderMarketsPage(); });
    $("#mkt-next", el).addEventListener("click", () => { page++; renderMarketsPage(); });
    $("#mkt-page-input", el).addEventListener("change", (e) => {
      const v = parseInt(e.target.value, 10);
      page = Number.isFinite(v) ? v : 1;
      renderMarketsPage();
    });
  }

  // Any change to filter/search/sort/tribute selection invalidates the
  // current page, so jump back to page 1; Prev/Next/page-input leave it alone.
  function applyFilters() { page = 1; renderMarketsPage(); }

  renderMarketsPage();

  const searchEl = $("#mkt-search", view);
  searchEl.addEventListener("input", () => { searchQ = searchEl.value.trim(); applyFilters(); });

  const sortEl = $("#mkt-sort", view);
  sortEl.addEventListener("change", () => { sortBy = sortEl.value; applyFilters(); });

  view.querySelectorAll(".cat-pill").forEach((pill) =>
    pill.addEventListener("click", () => {
      view.querySelectorAll(".cat-pill").forEach((p) => p.classList.remove("active"));
      pill.classList.add("active");
      activeCat = pill.dataset.cat;
      applyFilters();
    }));

  // Tribute picker: multi-select dropdown, "select all that apply".
  const tributeBtn   = $("#tribute-filter-btn", view);
  const tributeMenu  = $("#tribute-filter-menu", view);
  const tributeCount = $("#tribute-filter-count", view);
  const tributeClear = $("#tribute-filter-clear", view);

  if (tributeBtn) {
    tributeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      tributeMenu.hidden = !tributeMenu.hidden;
    });
  }
  view.querySelectorAll('.tribute-filter-item input[type="checkbox"]').forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.checked) selectedTributeIds.add(cb.value); else selectedTributeIds.delete(cb.value);
      tributeCount.textContent = selectedTributeIds.size ? `(${selectedTributeIds.size})` : "";
      applyFilters();
    }));
  if (tributeClear) {
    tributeClear.addEventListener("click", () => {
      selectedTributeIds.clear();
      view.querySelectorAll('.tribute-filter-item input[type="checkbox"]').forEach((cb) => { cb.checked = false; });
      tributeCount.textContent = "";
      applyFilters();
    });
  }
}

// Closes the tribute picker dropdown when clicking outside it. Bound once at
// module scope (not per viewMarkets() render) since the elements only exist
// while the Markets tab is mounted — the lookups simply no-op on other tabs.
document.addEventListener("click", (e) => {
  const menu = document.getElementById("tribute-filter-menu");
  const wrap = document.getElementById("tribute-filter");
  if (menu && !menu.hidden && wrap && !wrap.contains(e.target)) menu.hidden = true;
});

function tailPath(t) {
  return t.kind === "member" ? `/tail/parlay/${t.id}` : `/tail/${t.id}`;
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
        <button class="btn btn-primary btn-sm" data-act="tail" data-id="${t.id}" data-kind="${esc(t.kind || "template")}" data-odds="${t.combined_odds ?? ""}">Tail this</button>
        <button class="btn btn-outline btn-sm" data-act="add-slip" data-id="${t.id}" data-kind="${esc(t.kind || "template")}">Add to Slip</button>
      </div>
    </div>`;
}

function bindFeaturedParlayActions(view) {
  view.querySelectorAll('[data-act="tail"]').forEach((b) =>
    b.addEventListener("click", () => openWagerModal({
      title: "Tail Parlay",
      odds: b.dataset.odds !== "" ? Number(b.dataset.odds) : null,
      onSubmit: (wager) => api(tailPath(b.dataset), { method: "POST", body: { wager } }),
      after: () => { location.hash = "#mybets"; },
    })));
  view.querySelectorAll('[data-act="add-slip"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`${tailPath(b.dataset)}/add-to-slip`, { method: "POST" });
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

function tributeStatCells(t) {
  const stats = [
    ["Training", t.training_score != null ? String(t.training_score) : "—"],
    ["Kills", String(t.kills ?? 0)],
    ["Alliance", t.alliance ? esc(t.alliance) : "—"],
  ];
  if (t.placement != null) stats.push(["Placement", `#${t.placement}`]);
  return stats.map(([label, value]) => `
    <div class="tribute-stat">
      <span class="tribute-stat-label">${label}</span>
      <span class="tribute-stat-value">${value}</span>
    </div>`).join("");
}

// Shown at its natural aspect ratio (width:100%, height:auto — no cropping)
// so the card just grows to fit whatever image is provided, rather than
// squeezing a portrait photo into a small fixed-size thumbnail. Omitted
// entirely when no face claim is set, so undecorated tributes stay compact.
function tributePortrait(t) {
  if (!t.face_claim) return "";
  return `<img class="tribute-portrait" src="${esc(t.face_claim)}" alt="" loading="lazy" onerror="this.remove()">`;
}

async function viewTributes(view) {
  const { tributes } = await api("/tributes");
  view.innerHTML = `
    <div class="grid tribute-grid">
      ${tributes.map((t) => `
        <div class="card tribute-card status-edge-${esc(t.status.toLowerCase())}">
          <div class="tribute-head">
            <span class="district">D${t.district}</span>
            <span class="status status-${esc(t.status.toLowerCase())}">${esc(t.status)}</span>
          </div>
          ${tributePortrait(t)}
          <div class="tribute-content">
            <div class="tribute-name">${esc(t.name)}</div>
            <div class="dim tribute-sub">${esc(t.gender)}${t.age != null ? ` · Age ${t.age}` : ""}</div>
            <div class="tribute-stat-grid">${tributeStatCells(t)}</div>
            ${t.win_market_id && t.status === "ALIVE" ? `
              <div class="tribute-bet">
                <span class="${oddsClass(t.win_odds)}">${fmtOdds(t.win_odds)} to win</span>
                <button class="btn btn-primary btn-sm" data-act="bet" data-id="${t.win_market_id}">Bet</button>
              </div>` : ""}
          </div>
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

  // Bonus / boost breakdown line — the bonus stake is not returned on a win, so
  // payouts shown here are net of it.
  const promoLine = (stake, bonus, pct, rawPayout, boostedPayout) => {
    if (!bonus && !pct) return "";
    const parts = [];
    if (bonus) parts.push(`${fmtChips(bonus)} bonus + ${fmtChips(stake - bonus)} chips (bonus stake not returned)`);
    if (pct) parts.push(`+${pct}% boost: payout ${fmtChips(rawPayout - bonus)} &rarr; ${fmtChips(boostedPayout - bonus)}`);
    return `<div class="dim" style="font-size:0.85em">Stake ${fmtChips(stake)} — ${parts.join(" · ")}</div>`;
  };

  const straight = data.straight_bets.map((b) => `
    <div class="card bet-row">
      <div class="bet-main">
        <div class="bet-label">${esc(mlabel(b.market_id))}</div>
        <div class="dim">Wager ${fmtChips(b.wager)} @ ${fmtOdds(b.odds_at_placement)} · win ${fmtChips(b.payout_if_win - b.bonus_bet_amount)}</div>
        ${promoLine(b.wager, b.bonus_bet_amount, b.profit_boost_pct, b.raw_payout, b.payout_if_win)}
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
      <div class="dim">Wager ${fmtChips(p.total_wager)} · payout ${fmtChips(p.total_payout - p.bonus_bet_amount)}</div>
      ${promoLine(p.total_wager, p.bonus_bet_amount, p.profit_boost_pct, p.raw_total_payout, p.total_payout)}
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
  let promoInfo = { bonus_balance: 0, boosts: [] };
  try { promoInfo = await api("/my-boosts"); } catch (e) { /* non-fatal */ }
  const bonusBal = promoInfo.bonus_balance || 0;
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
          <input id="parlay-wager" type="number" min="0" placeholder="${bonusBal ? "Wager in chips (optional with bonus bets)" : "Wager (chips)"}" class="input">
          ${bonusBal ? `<input id="parlay-bonus" class="input" type="number" min="0" max="${bonusBal}" value="0" placeholder="Bonus bets to stake (max ${fmtChips(bonusBal)})">` : ""}
          ${promoInfo.boosts.length ? `<select id="parlay-boost" class="input">
            <option value="">No profit boost</option>
            ${promoInfo.boosts.map((b) => `<option value="${b.id}">${esc(b.label)}</option>`).join("")}
          </select>` : ""}
          <div class="modal-payout dim" id="parlay-payout"></div>
          <label class="checkbox"><input type="checkbox" id="parlay-public" checked> List on tail board</label>
          <div class="row-buttons">
            <button class="btn btn-primary" id="parlay-go">Submit Parlay</button>
            ${ME?.is_admin ? `<button class="btn btn-outline" id="parlay-feature">Feature Parlay</button>` : ""}
            <button class="btn btn-outline" id="parlay-clear">Clear</button>
          </div>
        </div>` : (legs.length ? `<button class="btn btn-outline" id="parlay-clear">Clear slip</button>` : "")}
    </div>`;

  view.querySelectorAll('[data-act="remove-leg"]').forEach((b) =>
    b.addEventListener("click", () => doAction(`/parlay/remove/${b.dataset.id}`, "POST")));
  const clear = $("#parlay-clear", view);
  if (clear) clear.addEventListener("click", () => doAction("/parlay/clear", "POST"));
  const feature = $("#parlay-feature", view);
  if (feature) feature.addEventListener("click", () => openFeatureParlayModal());
  const wagerEl = $("#parlay-wager", view);
  const pBonusEl = $("#parlay-bonus", view);
  const pBoostEl = $("#parlay-boost", view);
  if (wagerEl && data.combined_odds != null) {
    const recalc = () => {
      const w = Number(wagerEl.value) || 0;
      const bonus = pBonusEl ? (Number(pBonusEl.value) || 0) : 0;
      const stake = w + bonus;
      let payout = payoutForWager(stake, data.combined_odds);
      let pct = 0;
      if (pBoostEl && pBoostEl.value) {
        const b = promoInfo.boosts.find((x) => String(x.id) === pBoostEl.value);
        if (b) pct = b.pct;
      }
      // client preview only — server applies the boost per matching leg
      if (pct) payout = stake + Math.round((payout - stake) * (1 + pct / 100));
      payout = Math.min(payout, parlayPayoutCap());
      const shown = payout - bonus;
      $("#parlay-payout", view).textContent = stake
        ? `Win ~${fmtChips(shown)} chips${pct ? ` (+${pct}% boost, applied to matching legs)` : ""}${bonus ? ` — winnings only, bonus stake not returned` : ""}`
        : "";
    };
    wagerEl.addEventListener("input", recalc);
    if (pBonusEl) pBonusEl.addEventListener("input", recalc);
    if (pBoostEl) pBoostEl.addEventListener("change", recalc);
  }
  const go = $("#parlay-go", view);
  if (go) go.addEventListener("click", async () => {
    const wager = Number($("#parlay-wager", view).value) || 0;
    const is_public = $("#parlay-public", view).checked;
    const bonusV = pBonusEl ? (Number(pBonusEl.value) || 0) : 0;
    if (wager < 1 && bonusV < 1) return toast("Enter a wager or apply bonus bets.", "error");
    const body = { wager, is_public };
    if (bonusV > 0) body.bonus_amount = bonusV;
    if (pBoostEl && pBoostEl.value) body.profit_boost_token_id = Number(pBoostEl.value);
    try {
      const r = await api("/parlay/submit", { method: "POST", body });
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
            <button class="btn btn-primary btn-sm" data-act="tail" data-id="${t.id}" data-kind="${esc(t.kind || "template")}" data-odds="${t.combined_odds ?? ""}">Tail this</button>
            <button class="btn btn-outline btn-sm" data-act="add-slip" data-id="${t.id}" data-kind="${esc(t.kind || "template")}">Add to Slip</button>
          </div>
        </div>`).join("") : `<div class="empty">No public parlays to tail right now.</div>`}
    </div>`;

  view.querySelectorAll('[data-act="tail"]').forEach((b) =>
    b.addEventListener("click", () => openWagerModal({
      title: "Tail Parlay",
      odds: b.dataset.odds !== "" ? Number(b.dataset.odds) : null,
      onSubmit: (wager) => api(tailPath(b.dataset), { method: "POST", body: { wager } }),
      after: () => { location.hash = "#mybets"; },
    })));
  view.querySelectorAll('[data-act="add-slip"]').forEach((b) =>
    b.addEventListener("click", async () => {
      try {
        const r = await api(`${tailPath(b.dataset)}/add-to-slip`, { method: "POST" });
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
        ${meData.bonus_bet_balance ? `<div class="odds-pos" id="bonus-toggle" role="button" tabindex="0" style="margin-top:6px;cursor:pointer;text-decoration:underline dotted">${fmtChips(meData.bonus_bet_balance)} bonus bets ▸</div>
          <div class="dim" style="font-size:0.8rem">${meData.bonus_bet_next_expiry ? "next expiry " + meData.bonus_bet_next_expiry.slice(0, 10) : "no expiry"}</div>
          <div id="bonus-breakdown" hidden style="margin-top:8px;text-align:left"></div>` : ""}
      </div>
      <div class="card">
        <table class="balance-stats-table">
          <tr><td class="dim">Total Wagered</td><td class="chips">${fmtChips(meData.total_wagered)}</td></tr>
          <tr><td class="dim">Total Won</td><td class="odds-pos">${fmtChips(meData.total_won)}</td></tr>
          <tr><td class="dim">ROI</td><td class="${roi >= 0 ? "odds-pos" : "odds-neg"}">${roi >= 0 ? "+" : ""}${roi}%</td></tr>
          ${meData.bonus_wagered ? `<tr><td class="dim">Bonus Wagered</td><td>${fmtChips(meData.bonus_wagered)}</td></tr>` : ""}
          ${meData.bonus_won ? `<tr><td class="dim">Bonus Won</td><td class="odds-pos">${fmtChips(meData.bonus_won)}</td></tr>` : ""}
          <tr><td class="dim">Straight Bets</td><td>${straight.length}</td></tr>
          <tr><td class="dim">Parlays</td><td>${parlays.length}</td></tr>
          <tr><td class="dim">Win Rate</td><td>${winRate}</td></tr>
        </table>
      </div>
    </div>
    ${(meData.active_boosts && meData.active_boosts.length) ? `
      <h2 class="section-title">Your Profit Boosts</h2>
      <div class="list">
        ${meData.active_boosts.map((b) => {
          let s = b.scope_type === "DISTRICT" ? `District ${b.scope_id}` : b.scope_type === "ALLIANCE" ? `Alliance #${b.scope_id}` : "Any bet";
          return `<div class="card bet-row"><span class="odds-pos">+${b.pct}%</span> · ${s}${b.expires_at ? ` · <span class="dim">exp ${b.expires_at.slice(0, 10)}</span>` : ""}</div>`;
        }).join("")}
      </div>` : ""}
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

  const toggle = $("#bonus-toggle", view);
  if (toggle) {
    const panel = $("#bonus-breakdown", view);
    let loaded = false;
    const open = async () => {
      if (!loaded) {
        try {
          const d = await api("/my-bonus-lots");
          panel.innerHTML = d.lots.length
            ? d.lots.map((l) => `<div class="card bet-row" style="display:block">
                <span class="odds-pos">${fmtChips(l.amount_remaining)}</span>
                <span class="dim"> / ${fmtChips(l.original_amount)}</span> · ${esc(l.source)}
                <div class="dim" style="font-size:0.8rem">${l.expires_at ? "expires " + l.expires_at.slice(0, 10) : "no expiry"}</div>
              </div>`).join("")
            : `<div class="dim">No active bonus bets.</div>`;
        } catch (e) {
          panel.innerHTML = `<div class="dim">${esc(e.message)}</div>`;
        }
        loaded = true;
      }
      const showing = !panel.hidden;
      panel.hidden = showing;
      toggle.textContent = `${fmtChips(meData.bonus_bet_balance)} bonus bets ${showing ? "▸" : "▾"}`;
    };
    toggle.addEventListener("click", open);
    toggle.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    });
  }
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
      <a href="${base}#admin/parlays"  class="${sub === "parlays"  ? "active" : ""}">Parlays</a>
      <a href="${base}#admin/rates"    class="${sub === "rates"    ? "active" : ""}">Rates</a>
      <a href="${base}#admin/promos"   class="${sub === "promos"   ? "active" : ""}">Promos</a>
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
  if (sub === "parlays")  return adminParlays(body);
  if (sub === "rates")    return adminRates(body);
  if (sub === "promos")   return adminPromos(body);
  return adminMarkets(body);
}

async function adminPromos(body) {
  let data;
  try {
    data = await api("/admin/promos");
  } catch (e) {
    body.innerHTML = `<div class="card dim">${esc(e.message)}</div>`;
    return;
  }
  // promo timestamps come back as naive-UTC ISO (no zone) — render in local time
  const fmtDt = (iso) => {
    if (!iso) return "";
    const d = new Date(/[Z+]/.test(iso) ? iso : iso + "Z");
    return isNaN(d) ? iso : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const scopeSel = (name) => `
    <select id="${name}" class="input">
      <option value="USER">A user</option>
      <option value="FIRST_TOUCH">Every new member (first interaction)</option>
    </select>`;
  const channels = data.channels || [];
  const roles = data.roles || [];
  const channelField = channels.length
    ? `<select id="cd-channel" class="input">${channels.map((c) => `<option value="${c.id}">#${esc(c.name)}</option>`).join("")}</select>`
    : `<input id="cd-channel" class="input" placeholder="Channel ID">`;
  const roleField = roles.length
    ? `<select id="cd-role" class="input"><option value="">No role ping</option>${roles.map((r) => `<option value="${r.id}">@${esc(r.name)}</option>`).join("")}</select>`
    : `<input id="cd-role" class="input" placeholder="Ping role ID (optional)">`;
  body.innerHTML = `
    <div class="cd-row" style="display:flex;flex-wrap:wrap;gap:.6rem;margin-bottom:.6rem">
    <div class="card admin-form" style="flex:1 1 260px">
      <div class="card-label">POST A CLAIM DROP</div>
      <div class="dim" style="width:100%">Posts an embed with a Claim button in a channel. Any member who isn't blocked from betting can claim once.</div>
      ${channelField}
      ${roleField}
      <select id="cd-kind" class="input">
        <option value="BONUS">Bonus bets</option>
        <option value="BOOST">Profit boost</option>
      </select>
      <input id="cd-amount" class="input" type="number" min="1" value="2500" placeholder="Bonus amount (chips)">
      <select id="cd-tpl" class="input" style="display:none">
        ${data.templates.map((t) => `<option value="${t.id}">${esc(t.name)} (+${t.boost_pct}%, ${t.scope_type})</option>`).join("")}
      </select>
      <textarea id="cd-msg" class="input" rows="3" placeholder="Announcement text, shown inside the embed"></textarea>
      <input id="cd-max" class="input" type="number" min="1" placeholder="Max claims (blank = unlimited)">
      <input id="cd-dur" class="input" type="number" min="1" placeholder="Button open for N hours (blank = no limit)">
      <input id="cd-rexp" class="input" type="number" min="1" placeholder="Reward expires after N days (blank = permanent)">
      <button class="btn btn-primary" id="cd-go">Post Drop</button>
    </div>
    <div class="card" style="flex:1 1 260px">
      <div class="card-label">MESSAGE PREVIEW</div>
      <div id="cd-preview"></div>
    </div>
    </div>

    <div class="card admin-form">
      <div class="card-label">GRANT BONUS BETS</div>
      <div class="dim" style="width:100%">Free credit in chips. A win pays winnings only; a loss costs nothing; no cash value. To reach a role or the whole server, use <code>/promo drop</code> in Discord.</div>
      ${scopeSel("bg-scope")}
      <input id="bg-target" class="input" placeholder="Discord user ID (blank for first-interaction)">
      <input id="bg-amount" class="input" type="number" min="1" value="2500" placeholder="Amount (chips)">
      <input id="bg-days" class="input" type="number" min="1" placeholder="Valid days (blank = permanent)">
      <input id="bg-hours" class="input" type="number" min="1" placeholder="Valid hours (optional)">
      <input id="bg-note" class="input" placeholder="Note (optional)">
      <button class="btn btn-primary" id="bg-go">Grant Bonus</button>
    </div>

    <div class="card admin-form">
      <div class="card-label">DEDUCT BONUS BETS</div>
      <select id="bd-scope" class="input"><option value="USER">A user</option></select>
      <input id="bd-target" class="input" placeholder="Discord user ID">
      <input id="bd-amount" class="input" type="number" min="1" placeholder="Amount (blank = wipe balance)">
      <button class="btn btn-danger" id="bd-go">Deduct</button>
    </div>

    ${data.ft_bonus.length ? `<div class="card">
      <div class="card-label">FIRST-INTERACTION BONUS RULES</div>
      ${data.ft_bonus.map((g) => `<div class="list-row">
        <span>${fmtChips(g.amount)}${g.expiry_hours ? ` · ${g.expiry_hours}h` : ""}${g.note ? ` · ${esc(g.note)}` : ""}</span>
        <button class="btn btn-sm btn-danger" data-act="ft-del" data-id="${g.id}">Remove</button>
      </div>`).join("")}
    </div>` : ""}

    <div class="card">
      <div class="card-label">OUTSTANDING BONUS BALANCES</div>
      ${data.bonus_users.length ? data.bonus_users.map((e) => `<div class="list-row">
        <span>${esc(e.name)} <span class="dim">${e.uid}</span> — <span class="odds-pos">${fmtChips(e.total)}</span>${e.expiry ? ` <span class="dim">exp ${e.expiry.slice(0, 16).replace("T", " ")}</span>` : ""}</span>
        <button class="btn btn-sm btn-danger" data-act="bu-revoke" data-id="${e.uid}">Revoke all</button>
      </div>`).join("") : `<div class="dim">No outstanding bonus bets.</div>`}
    </div>

    <div class="card admin-form">
      <div class="card-label">NEW PROFIT BOOST TEMPLATE</div>
      <div class="dim" style="width:100%">Single-use token that lifts a wager's winnings by a %. Consumed on use, win or lose.</div>
      <input id="bt-name" class="input" placeholder="Name">
      <input id="bt-pct" class="input" type="number" min="1" value="50" placeholder="Boost %">
      <select id="bt-scope" class="input">
        <option value="ANY">Any bet</option>
        <option value="DISTRICT">A district</option>
        <option value="ALLIANCE">An alliance</option>
      </select>
      <input id="bt-scopeid" class="input" placeholder="District # / alliance ID (scoped only)">
      <input id="bt-maxwager" class="input" type="number" min="0" placeholder="Max wager (optional)">
      <label class="dim" style="width:100%"><input type="checkbox" id="bt-ft"> Grant to every new member on first interaction</label>
      <button class="btn btn-primary" id="bt-go">Create Template</button>
    </div>

    <div class="card">
      <div class="card-label">BOOST TEMPLATES</div>
      ${data.templates.length ? data.templates.map((t) => `<div class="list-row">
        <span>${esc(t.name)} — <span class="odds-pos">+${t.boost_pct}%</span> ${t.scope_type}${t.scope_id != null ? ` #${t.scope_id}` : ""}${t.max_wager ? ` · max ${fmtChips(t.max_wager)}` : ""}${t.grant_on_first_touch ? " · first-touch" : ""} ${t.active ? "" : "<span class='dim'>(disabled)</span>"}</span>
        <span>
          <button class="btn btn-sm" data-act="bt-toggle" data-id="${t.id}">${t.active ? "Disable" : "Enable"}</button>
          <button class="btn btn-sm btn-danger" data-act="bt-del" data-id="${t.id}">Delete</button>
        </span>
      </div>`).join("") : `<div class="dim">No templates yet.</div>`}
    </div>

    <div class="card admin-form">
      <div class="card-label">GRANT A BOOST</div>
      <select id="bgr-tpl" class="input">
        ${data.templates.map((t) => `<option value="${t.id}">${esc(t.name)} (+${t.boost_pct}%, ${t.scope_type})</option>`).join("")}
      </select>
      ${scopeSel("bgr-scope")}
      <input id="bgr-target" class="input" placeholder="Discord user ID (blank for first-interaction)">
      <input id="bgr-days" class="input" type="number" min="1" placeholder="Valid days (blank = no expiry)">
      <input id="bgr-hours" class="input" type="number" min="1" placeholder="Valid hours (optional)">
      <button class="btn btn-primary" id="bgr-go">Grant Boost</button>
    </div>

    <div class="card">
      <div class="card-label">OUTSTANDING BOOST TOKENS</div>
      ${data.tokens.length ? data.tokens.map((t) => `<div class="list-row">
        <span>${esc(t.name)} <span class="dim">${t.uid}</span> — <span class="odds-pos">+${t.boost_pct}%</span> ${t.scope_type}${t.scope_id != null ? ` #${t.scope_id}` : ""}${t.expires_at ? ` <span class="dim">exp ${t.expires_at.slice(0, 16).replace("T", " ")}</span>` : ""}</span>
        <button class="btn btn-sm btn-danger" data-act="tok-revoke" data-id="${t.id}">Revoke</button>
      </div>`).join("") : `<div class="dim">No outstanding tokens.</div>`}
    </div>

    <div class="card admin-form">
      <div class="card-label">NEW DEPOSIT MATCH PROMO</div>
      <div class="dim" style="width:100%">On "Mark Done" of a member's /deposit during the window, grant the match % as bonus bets, up to the per-member cap (cumulative).</div>
      <input id="dm-name" class="input" placeholder="Name">
      <input id="dm-pct" class="input" type="number" min="1" value="100" placeholder="Match %">
      <input id="dm-cap" class="input" type="number" min="1" value="5000" placeholder="Max match per member (bonus bets)">
      <input id="dm-bexp" class="input" type="number" min="1" placeholder="Matched bonus bets expire after N days (blank = permanent)">
      <input id="dm-start" class="input" type="datetime-local" placeholder="Starts (blank = now)">
      <input id="dm-end" class="input" type="datetime-local" placeholder="Ends">
      <input id="dm-role" class="input" placeholder="Restrict to role ID (optional)">
      <button class="btn btn-primary" id="dm-go">Create Promo</button>
    </div>

    <div class="card">
      <div class="card-label">DEPOSIT MATCH PROMOS</div>
      ${data.deposit_promos.length ? data.deposit_promos.map((p) => `<div class="list-row">
        <span>${esc(p.name)} ${p.live ? "<span class='odds-pos'>● live</span>" : ""} — ${p.match_pct}% up to ${fmtChips(p.max_match_per_user)} in bonus bets · <span class="dim">${p.match_bonus_expiry_days ? `${p.match_bonus_expiry_days}d expiry · ` : ""}${fmtDt(p.starts_at)} → ${fmtDt(p.ends_at)}${p.role_id ? ` · role ${p.role_id}` : ""} · ${p.claims.members} claim(s), ${fmtChips(p.claims.matched)} matched</span></span>
        <span>
          ${p.live ? `<button class="btn btn-sm" data-act="dm-end" data-id="${p.id}">End now</button>` : ""}
          <button class="btn btn-sm btn-danger" data-act="dm-del" data-id="${p.id}">Delete</button>
        </span>
      </div>`).join("") : `<div class="dim">No promos.</div>`}
    </div>`;

  const val = (id) => $("#" + id, body).value.trim();
  const num = (id) => Number($("#" + id, body).value) || 0;
  // <input type="datetime-local"> is local wall-clock with no zone; send it as
  // a UTC ISO string so the server (which compares against utcnow) reads it right.
  const isoUtc = (id) => {
    const v = $("#" + id, body).value;
    return v ? new Date(v).toISOString() : "";
  };
  const reload = () => adminPromos(body);
  const call = async (path, opts) => {
    try {
      const r = await api(path, opts);
      toast(r.message || "Done.");
      reload();
    } catch (e) { toast(e.message, "error"); }
  };
  // window.confirm() is inert in the Discord Activity's sandboxed iframe (no
  // allow-modals) — it returns false, so a confirm-gated handler silently does
  // nothing. Use a two-press "click again to confirm" on the button instead.
  const _armed = new WeakSet();
  const armConfirm = (btn, run) => {
    if (_armed.has(btn)) {
      _armed.delete(btn);
      btn.textContent = btn.dataset.confirmLabel || btn.textContent;
      run();
      return;
    }
    _armed.add(btn);
    btn.dataset.confirmLabel = btn.dataset.confirmLabel || btn.textContent;
    btn.textContent = "Confirm?";
    setTimeout(() => {
      if (_armed.has(btn)) { _armed.delete(btn); btn.textContent = btn.dataset.confirmLabel; }
    }, 4000);
  };

  const cdKind = $("#cd-kind", body);
  const cdRoleName = () => {
    const el = $("#cd-role", body);
    if (!el.value) return "";
    if (el.tagName === "SELECT") return (el.selectedOptions[0].textContent || "").replace(/^@/, "");
    return "role";
  };
  const cdRewardLabel = () => {
    if (cdKind.value === "BONUS") return `${fmtChips(num("cd-amount"))} bonus bets`;
    const t = data.templates.find((x) => String(x.id) === $("#cd-tpl", body).value);
    if (!t) return "a profit boost";
    const scope = t.scope_type === "DISTRICT" ? `District ${t.scope_id}`
      : t.scope_type === "ALLIANCE" ? `Alliance #${t.scope_id}` : "any bet";
    return `+${t.boost_pct}% profit boost (${scope})`;
  };
  const renderCdPreview = () => {
    const p = $("#cd-preview", body);
    const msg = $("#cd-msg", body).value.trim();
    const roleName = cdRoleName();
    let contains = cdRewardLabel();
    const rexp = num("cd-rexp"), maxc = num("cd-max");
    const extras = [];
    if (rexp) extras.push(`expires ${rexp}d after you claim`);
    if (maxc) extras.push(`first ${fmtChips(maxc)} claimers only`);
    if (extras.length) contains += `<br><span class="dim">${esc(extras.join(" · "))}</span>`;
    p.innerHTML = `
      ${roleName ? `<div style="color:var(--cashed-out);font-weight:600;margin-bottom:.3rem">@${esc(roleName)}</div>` : ""}
      <div style="border-left:4px solid var(--header-gold);background:var(--bg-mid);border-radius:4px;padding:.5rem .6rem">
        <div style="font-weight:700;color:var(--text-white);margin-bottom:.25rem">🎁 Promo Drop</div>
        <div style="white-space:pre-wrap">${esc(msg) || `<span class="dim">Your announcement text appears here…</span>`}</div>
        <div style="margin-top:.5rem;font-size:.72rem;font-weight:700;color:var(--text-dim);letter-spacing:.05em">THIS DROP CONTAINS</div>
        <div>${contains}</div>
        <div class="dim" style="margin-top:.4rem;font-size:.75rem">Panem Sportsbook — press Claim below.</div>
      </div>
      <button class="btn btn-primary" disabled style="margin-top:.4rem;background:var(--won);border-color:var(--won)">🎁 Claim</button>`;
  };
  const syncCd = () => {
    const boost = cdKind.value === "BOOST";
    $("#cd-amount", body).style.display = boost ? "none" : "";
    $("#cd-tpl", body).style.display = boost ? "" : "none";
    renderCdPreview();
  };
  ["cd-kind", "cd-role", "cd-amount", "cd-tpl", "cd-msg", "cd-max", "cd-rexp"].forEach((id) => {
    const el = $("#" + id, body);
    el.addEventListener("input", renderCdPreview);
    el.addEventListener("change", renderCdPreview);
  });
  cdKind.addEventListener("change", syncCd);
  syncCd();
  $("#cd-go", body).addEventListener("click", () => {
    const msg = $("#cd-msg", body).value.trim();
    if (!msg) { toast("Enter a message.", "error"); return; }
    call("/admin/promos/claim-drop", {
      method: "POST",
      body: {
        channel_id: val("cd-channel"), reward_kind: cdKind.value,
        ping_role_id: val("cd-role"),
        bonus_amount: num("cd-amount"),
        boost_template_id: Number($("#cd-tpl", body).value) || 0,
        message: msg, max_claims: num("cd-max"),
        duration_hours: num("cd-dur"), reward_expiry_days: num("cd-rexp"),
      },
    });
  });
  $("#bg-go", body).addEventListener("click", () => call("/admin/promos/bonus/grant", {
    method: "POST",
    body: { scope: val("bg-scope"), target_id: val("bg-target"), amount: num("bg-amount"),
      expiry_days: num("bg-days"), expiry_hours: num("bg-hours"), note: val("bg-note") },
  }));
  $("#bd-go", body).addEventListener("click", (ev) => armConfirm(ev.currentTarget, () =>
    call("/admin/promos/bonus/deduct", { method: "POST",
      body: { scope: val("bd-scope"), target_id: val("bd-target"), amount: num("bd-amount") } })));
  $("#bt-go", body).addEventListener("click", () => call("/admin/promos/boost-template", {
    method: "POST",
    body: { name: val("bt-name"), boost_pct: num("bt-pct"), scope_type: val("bt-scope"),
      scope_id: val("bt-scopeid"), max_wager: val("bt-maxwager"), grant_on_first_touch: $("#bt-ft", body).checked },
  }));
  $("#bgr-go", body).addEventListener("click", () => call("/admin/promos/boost/grant", {
    method: "POST",
    body: { template_id: Number($("#bgr-tpl", body).value), scope: val("bgr-scope"),
      target_id: val("bgr-target"), expiry_days: num("bgr-days"), expiry_hours: num("bgr-hours") },
  }));
  $("#dm-go", body).addEventListener("click", () => call("/admin/promos/deposit-match", {
    method: "POST",
    body: { name: val("dm-name"), match_pct: num("dm-pct"), max_match_per_user: num("dm-cap"),
      match_bonus_expiry_days: num("dm-bexp"),
      starts_at: isoUtc("dm-start"), ends_at: isoUtc("dm-end"), role_id: val("dm-role") },
  }));
  body.querySelectorAll("[data-act]").forEach((btn) => {
    const id = btn.dataset.id;
    const map = {
      "ft-del": ["/admin/promos/bonus/first-touch/" + id, "DELETE"],
      "bu-revoke": ["/admin/promos/bonus/user/" + id + "/revoke", "POST"],
      "bt-toggle": ["/admin/promos/boost-template/" + id + "/toggle", "POST"],
      "bt-del": ["/admin/promos/boost-template/" + id, "DELETE"],
      "tok-revoke": ["/admin/promos/boost/token/" + id + "/revoke", "POST"],
      "dm-end": ["/admin/promos/deposit-match/" + id + "/end", "POST"],
      "dm-del": ["/admin/promos/deposit-match/" + id, "DELETE"],
    };
    btn.addEventListener("click", () => {
      const m = map[btn.dataset.act];
      if (!m) return;
      if (btn.dataset.act.endsWith("-del") || btn.dataset.act === "bu-revoke") {
        armConfirm(btn, () => call(m[0], { method: m[1] }));
      } else {
        call(m[0], { method: m[1] });
      }
    });
  });
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
      <div class="sort-row">
        <label class="sort-label" for="m-sort">Sort</label>
        <select class="input sort-select" id="m-sort">${SORT_OPTIONS_HTML}</select>
      </div>
    </div>
    <div class="list" id="admin-market-list"></div>`;

  function renderList() {
    const listEl = $("#admin-market-list", body);
    const sorted = sortMarketList(all, adminMarketsSort);
    listEl.innerHTML = sorted.length
      ? sorted.map((m) => marketCard(m, { actions: "admin" })).join("")
      : `<div class="empty">No markets.</div>`;
    bindAdminMarketActions(body);
  }

  $("#m-recalc", body).addEventListener("click", async () => {
    if (!confirm("Recalculate odds on all non-overridden markets?")) return;
    await doAction("/admin/markets/recalc", "POST");
  });
  $("#m-bulk-close", body).addEventListener("click", async () => {
    if (!confirm("Close all open markets?")) return;
    await doAction("/admin/markets/bulk-close", "POST");
  });
  const sortEl = $("#m-sort", body);
  sortEl.value = adminMarketsSort;
  sortEl.addEventListener("change", (e) => { adminMarketsSort = e.target.value; renderList(); });

  renderList();
}

function bindAdminMarketActions(body) {
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
  const [{ users }, economy, payoutCaps] = await Promise.all([
    api("/admin/users"),
    api("/admin/economy").catch(() => null),
    api("/admin/payout-caps").catch(() => null),
  ]);
  body.innerHTML = `
    ${economy ? `
    <div class="card">
      <div class="card-label">ECONOMY</div>
      <div class="econ-grid">
        <div class="econ-item">
          <span class="econ-num">${fmtChips(economy.chips_spent)}</span>
          <span class="econ-label">Chips Spent</span>
        </div>
        <div class="econ-item">
          <span class="econ-num">${fmtChips(economy.chips_circulating)}</span>
          <span class="econ-label">In Circulation</span>
        </div>
        <div class="econ-item">
          <span class="econ-num odds-pos">${fmtChips(economy.panars_converted)}</span>
          <span class="econ-label">Panars Converted</span>
        </div>
        <div class="econ-item">
          <span class="econ-num odds-neg">${fmtChips(economy.chips_withdrawn)}</span>
          <span class="econ-label">Chips Withdrawn</span>
        </div>
        <div class="econ-item">
          <span class="econ-num ${economy.net_panars >= 0 ? "odds-pos" : "odds-neg"}">${economy.net_panars >= 0 ? "+" : ""}${fmtChips(economy.net_panars)}</span>
          <span class="econ-label">Net Panars</span>
        </div>
      </div>
    </div>` : ""}
    ${payoutCaps ? `
    <div class="card">
      <div class="card-label">PAYOUT CAPS</div>
      <div class="econ-grid">
        <div class="econ-item">
          <span class="econ-num">${fmtChips(payoutCaps.single_payout_cap)}</span>
          <span class="econ-label">Single Bet Cap</span>
        </div>
        <div class="econ-item">
          <span class="econ-num">${fmtChips(payoutCaps.parlay_payout_cap)}</span>
          <span class="econ-label">Parlay Cap</span>
        </div>
      </div>
      <div class="dim" style="margin-top:8px">Adjust these on the <a href="${location.pathname}${location.search}#admin/rates">Rates</a> tab.</div>
    </div>` : ""}
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
  body.innerHTML = `<div class="grid tribute-grid">
    ${tributes.map((t) => `
      <div class="card tribute-card status-edge-${esc(t.status.toLowerCase())}">
        <div class="tribute-head">
          <span class="district">D${t.district}</span>
          <span class="status status-${esc(t.status.toLowerCase())}">${esc(t.status)}</span>
        </div>
        ${tributePortrait(t)}
        <div class="tribute-content">
          <div class="tribute-name">${esc(t.name)}</div>
          <div class="dim tribute-sub">${esc(t.gender)}${t.age != null ? ` · Age ${t.age}` : ""}</div>
          ${t.status === "ALIVE" ? `
            <div class="row-buttons">
              <button class="btn btn-outline btn-sm" data-act="kill"   data-id="${t.id}" data-name="${esc(t.name)}">Eliminate</button>
              <button class="btn btn-primary btn-sm" data-act="victor" data-id="${t.id}" data-name="${esc(t.name)}">Victor</button>
            </div>` : ""}
          ${t.status === "DEAD" ? `
            <div class="row-buttons">
              <button class="btn btn-outline btn-sm" data-act="unkill" data-id="${t.id}" data-name="${esc(t.name)}">Unkill</button>
            </div>` : ""}
        </div>
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

async function adminParlays(body) {
  const [{ templates }, openData] = await Promise.all([
    api("/admin/parlays"),
    api("/markets?status=open"),
  ]);
  const openMarkets = openData.markets;

  body.innerHTML = `
    <div class="card admin-form" id="parlay-form">
      <div class="card-label">CREATE FEATURED PARLAY</div>
      <input id="p-name" class="input" placeholder="Name (required)" maxlength="100">
      <input id="p-desc" class="input" placeholder="Description (optional)" maxlength="500">
      <button class="btn btn-primary" id="p-add">Create Template</button>
    </div>
    <div class="list" id="parlay-list">
      ${templates.length ? templates.map((t) => `
        <div class="card tail-card">
          <div class="tail-head">
            <span class="tail-name">${esc(t.name)}</span>
            ${t.difficulty ? `<span class="badge">${esc(t.difficulty)}</span>` : ""}
            <span class="status ${t.active ? "status-open" : "status-closed"}">${t.active ? "ACTIVE" : "INACTIVE"}</span>
          </div>
          ${t.description ? `<div class="dim">${esc(t.description)}</div>` : ""}
          <ul class="parlay-legs">
            ${t.legs.length ? t.legs.map((l) => `
              <li>
                <span>${esc(l.label)}</span>
                <span class="${oddsClass(l.odds)}">${fmtOdds(l.odds)}</span>
                <button class="btn btn-outline btn-sm" data-act="p-remove-leg" data-tpl="${t.id}" data-leg="${l.leg_id}" style="margin-left:auto">Remove</button>
              </li>`).join("") : `<li class="dim">No legs yet — add one below.</li>`}
          </ul>
          <div class="row-buttons">
            <select class="input p-leg-picker" data-tpl="${t.id}" style="flex:1;min-width:140px">
              <option value="">Add a market as a leg…</option>
              ${openMarkets.filter((m) => !t.legs.some((l) => l.market_id === m.id)).map((m) => `
                <option value="${m.id}">${esc(m.label)} (${fmtOdds(m.odds)})</option>`).join("")}
            </select>
            <button class="btn btn-outline btn-sm" data-act="p-add-leg" data-tpl="${t.id}">Add Leg</button>
          </div>
          <div class="row-buttons">
            <span class="${oddsClass(t.combined_odds)}" style="flex:1;align-self:center;font-weight:700">${t.combined_odds == null ? "—" : fmtOdds(t.combined_odds)}</span>
            <button class="btn btn-outline btn-sm" data-act="p-toggle" data-tpl="${t.id}">${t.active ? "Deactivate" : "Activate"}</button>
            <button class="btn btn-outline btn-sm" data-act="p-delete" data-tpl="${t.id}">Delete</button>
          </div>
        </div>`).join("") : `<div class="empty">No featured parlays yet — create one above.</div>`}
    </div>`;

  $("#p-add", body).addEventListener("click", async () => {
    const name = $("#p-name", body).value.trim();
    const description = $("#p-desc", body).value.trim();
    if (!name) return toast("Name is required.", "error");
    try {
      const r = await api("/admin/parlays/create", { method: "POST", body: { name, description } });
      toast(r.message);
      adminParlays(body);
    } catch (e) { toast(e.message, "error"); }
  });

  body.querySelectorAll('[data-act="p-add-leg"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      const tplId = btn.dataset.tpl;
      const picker = body.querySelector(`.p-leg-picker[data-tpl="${tplId}"]`);
      const marketId = picker ? Number(picker.value) : 0;
      if (!marketId) return toast("Pick a market first.", "error");
      try {
        const r = await api(`/admin/parlays/${tplId}/add-leg`, { method: "POST", body: { market_id: marketId } });
        toast(r.message);
        adminParlays(body);
      } catch (e) { toast(e.message, "error"); }
    }));

  body.querySelectorAll('[data-act="p-remove-leg"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/parlays/${btn.dataset.tpl}/remove-leg`, { method: "POST", body: { leg_id: Number(btn.dataset.leg) } });
        toast(r.message);
        adminParlays(body);
      } catch (e) { toast(e.message, "error"); }
    }));

  body.querySelectorAll('[data-act="p-toggle"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/parlays/${btn.dataset.tpl}/toggle`, { method: "POST" });
        toast(r.message);
        adminParlays(body);
      } catch (e) { toast(e.message, "error"); }
    }));

  body.querySelectorAll('[data-act="p-delete"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this featured parlay template? This can't be undone.")) return;
      try {
        const r = await api(`/admin/parlays/${btn.dataset.tpl}`, { method: "DELETE" });
        toast(r.message);
        adminParlays(body);
      } catch (e) { toast(e.message, "error"); }
    }));
}

async function adminRates(body) {
  const [rates, { blocks }, payoutCaps, houseCut] = await Promise.all([
    api("/admin/exchange-rates"),
    api("/admin/public-blocks"),
    api("/admin/payout-caps"),
    api("/admin/house-cut"),
  ]);

  body.innerHTML = `
    <div class="card admin-form">
      <div class="card-label">GLOBAL PAYOUT CAPS</div>
      <input id="pc-single" class="input" type="number" min="1" value="${payoutCaps.single_payout_cap}" placeholder="Single bet cap (chips)">
      <input id="pc-parlay" class="input" type="number" min="1" value="${payoutCaps.parlay_payout_cap}" placeholder="Parlay cap (chips)">
      <button class="btn btn-primary" id="pc-save">Save Payout Caps</button>
      <div class="dim" style="width:100%">A wager that would pay out more than the applicable cap is rejected with the max wager the member could place instead.</div>
    </div>

    <div class="card admin-form">
      <div class="card-label">HOUSE CUT</div>
      <input id="hc-global" class="input" type="number" step="0.1" min="0" max="100" value="${houseCut.global_pct}" placeholder="House cut % of winning profit">
      <input id="hc-threshold" class="input" type="number" step="1" min="1" value="${houseCut.high_odds_threshold ?? ""}" placeholder="High-odds surcharge threshold (blank = off)">
      <input id="hc-highpct" class="input" type="number" step="0.1" min="0" max="100" value="${houseCut.high_odds_pct}" placeholder="High-odds surcharge %">
      <button class="btn btn-primary" id="hc-save">Save House Cut</button>
      <div class="dim" style="width:100%">Skimmed from the profit (payout − stake) of every WON bet/parlay. Winners paying above +threshold get the surcharge % instead (higher cut wins). Taken to date: ${fmtChips(houseCut.total_taken)}.</div>
    </div>

    <div class="card admin-form">
      <div class="card-label">PER-MARKET-TYPE HOUSE CUT</div>
      <div class="dim" style="width:100%">Overrides the global cut for straight bets on one market type (parlays always use the global rate).</div>
      <select id="hct-type" class="input">
        <option value="">— market type —</option>
        ${houseCut.market_types.map((mt) => `<option value="${esc(mt.value)}">${esc(mt.label)}</option>`).join("")}
      </select>
      <input id="hct-pct" class="input" type="number" step="0.1" min="0" max="100" value="0" placeholder="Cut %">
      <button class="btn btn-primary" id="hct-add">Set Override</button>
    </div>

    <div class="list">
      ${houseCut.by_type.length ? houseCut.by_type.map((o) => `
        <div class="card tail-card">
          <div class="tail-head">
            <span class="tail-name">${esc(o.label)}</span>
            <span class="badge">${o.pct}%</span>
          </div>
          <div class="row-buttons">
            <button class="btn btn-outline btn-sm" data-act="hct-remove" data-type="${esc(o.type)}">Remove</button>
          </div>
        </div>`).join("") : `<div class="empty">No per-type house-cut overrides.</div>`}
    </div>

    <div class="card admin-form">
      <div class="card-label">GLOBAL RATES</div>
      <input id="r-deposit" class="input" type="number" step="0.01" min="0.01" value="${rates.global_deposit_rate}" placeholder="Deposit rate (chips per Panar)">
      <input id="r-withdraw" class="input" type="number" step="0.01" min="0.01" value="${rates.global_withdraw_rate}" placeholder="Withdraw rate (Panars per chip)">
      <input id="r-payout" class="input" type="number" step="0.01" min="0.01" value="${rates.global_payout_rate}" placeholder="Payout multiplier (won bets & parlays)">
      <button class="btn btn-primary" id="r-save-global">Save Global Rates</button>
    </div>

    <div class="card admin-form">
      <div class="card-label">ADD PAYOUT MULTIPLIER OVERRIDE</div>
      <select id="r-scope" class="input">
        <option value="USER">User</option>
        <option value="ROLE">Role</option>
      </select>
      <input id="r-target" class="input" placeholder="Discord user ID or Role ID">
      <input id="r-rate" class="input" type="number" step="0.01" min="0.01" value="1.0" placeholder="Multiplier (1.1 = +10% payout)">
      <button class="btn btn-primary" id="r-add-override">Add Override</button>
      <div class="dim" style="width:100%">Multiplies a won bet/parlay's payout for this role/user. Resolved user &gt; highest role &gt; the global payout multiplier. Does not affect /deposit or /withdraw.</div>
    </div>

    <div class="list">
      ${rates.overrides.length ? rates.overrides.map((o) => `
        <div class="card tail-card">
          <div class="tail-head">
            <span class="tail-name">${o.scope} ${esc(o.target_id)}</span>
            <span class="badge">${o.direction === "PAYOUT" ? "PAYOUT ×" : o.direction + " (inert)"}</span>
          </div>
          <div class="row-buttons">
            <span style="flex:1;align-self:center;font-weight:700">${o.rate}</span>
            <button class="btn btn-outline btn-sm" data-act="r-remove" data-id="${o.id}">Remove</button>
          </div>
        </div>`).join("") : `<div class="empty">No payout multiplier overrides — everyone uses the global payout multiplier above.</div>`}
    </div>

    <div class="card admin-form">
      <div class="card-label">BLOCK PUBLIC PARLAYS</div>
      <select id="b-scope" class="input">
        <option value="USER">User</option>
        <option value="ROLE">Role</option>
      </select>
      <input id="b-target" class="input" placeholder="Discord user ID or Role ID">
      <button class="btn btn-primary" id="b-add">Block</button>
      <div class="dim" style="width:100%">Blocked members can still bet — their public/tail-board submissions are just kept private instead.</div>
    </div>

    <div class="list">
      ${blocks.length ? blocks.map((b) => `
        <div class="card tail-card">
          <div class="row-buttons">
            <span style="flex:1;align-self:center;font-weight:700">${b.scope} ${esc(b.target_id)}</span>
            <button class="btn btn-outline btn-sm" data-act="b-remove" data-id="${b.id}">Unblock</button>
          </div>
        </div>`).join("") : `<div class="empty">No public-parlay blocks set.</div>`}
    </div>`;

  $("#pc-save", body).addEventListener("click", async () => {
    const single_payout_cap = Number($("#pc-single", body).value);
    const parlay_payout_cap = Number($("#pc-parlay", body).value);
    if (!single_payout_cap || !parlay_payout_cap) return toast("Enter both payout caps.", "error");
    try {
      const r = await api("/admin/payout-caps", { method: "POST", body: { single_payout_cap, parlay_payout_cap } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  $("#hc-save", body).addEventListener("click", async () => {
    const global_pct = Number($("#hc-global", body).value);
    const rawThreshold = $("#hc-threshold", body).value.trim();
    const high_odds_threshold = rawThreshold === "" ? null : Number(rawThreshold);
    const high_odds_pct = Number($("#hc-highpct", body).value);
    try {
      const r = await api("/admin/house-cut", { method: "POST", body: { global_pct, high_odds_threshold, high_odds_pct } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  $("#hct-add", body).addEventListener("click", async () => {
    const market_type = $("#hct-type", body).value;
    const pct = Number($("#hct-pct", body).value);
    if (!market_type) return toast("Pick a market type.", "error");
    try {
      const r = await api("/admin/house-cut/type", { method: "POST", body: { market_type, pct } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  body.querySelectorAll('[data-act="hct-remove"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api("/admin/house-cut/type", { method: "POST", body: { market_type: btn.dataset.type, pct: null } });
        toast(r.message);
        adminRates(body);
      } catch (e) { toast(e.message, "error"); }
    }));

  $("#r-save-global", body).addEventListener("click", async () => {
    const deposit_rate = Number($("#r-deposit", body).value);
    const withdraw_rate = Number($("#r-withdraw", body).value);
    const payout_rate = Number($("#r-payout", body).value);
    if (!deposit_rate || !withdraw_rate || !payout_rate) return toast("Enter all three rates.", "error");
    try {
      const r = await api("/admin/exchange-rates/global", { method: "POST", body: { deposit_rate, withdraw_rate, payout_rate } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  $("#r-add-override", body).addEventListener("click", async () => {
    const scope = $("#r-scope", body).value;
    const target_id = $("#r-target", body).value.trim();
    const rate = Number($("#r-rate", body).value);
    if (!target_id || !rate) return toast("Enter a target ID and multiplier.", "error");
    try {
      const r = await api("/admin/exchange-rates", { method: "POST", body: { scope, target_id, direction: "PAYOUT", rate } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  body.querySelectorAll('[data-act="r-remove"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/exchange-rates/${btn.dataset.id}`, { method: "DELETE" });
        toast(r.message);
        adminRates(body);
      } catch (e) { toast(e.message, "error"); }
    }));

  $("#b-add", body).addEventListener("click", async () => {
    const scope = $("#b-scope", body).value;
    const target_id = $("#b-target", body).value.trim();
    if (!target_id) return toast("Enter a target ID.", "error");
    try {
      const r = await api("/admin/public-blocks", { method: "POST", body: { scope, target_id } });
      toast(r.message);
      adminRates(body);
    } catch (e) { toast(e.message, "error"); }
  });

  body.querySelectorAll('[data-act="b-remove"]').forEach((btn) =>
    btn.addEventListener("click", async () => {
      try {
        const r = await api(`/admin/public-blocks/${btn.dataset.id}`, { method: "DELETE" });
        toast(r.message);
        adminRates(body);
      } catch (e) { toast(e.message, "error"); }
    }));
}

// ── Generic helpers / modals ───────────────────────────────────────────────────

async function doAction(path, method = "POST", body) {
  // route() rebuilds #view from scratch, which wipes its scroll position —
  // save/restore it so an admin action (e.g. closing a market) doesn't jump
  // the list back to the top.
  const view = $("#view");
  const scrollTop = view ? view.scrollTop : 0;
  try {
    const r = await api(path, { method, body });
    if (r.message) toast(r.message);
    await refreshMe();
    await route();
    if (view) view.scrollTop = scrollTop;
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
  let promoInfo = { bonus_balance: 0, boosts: [] };
  try { promoInfo = await api(`/my-boosts?market_id=${marketId}`); } catch (e) { /* non-fatal */ }
  const bonusBal = promoInfo.bonus_balance || 0;
  const overlay = modal(`
    <h3>${esc(m.label)}</h3>
    <div class="dim">Odds <span class="${oddsClass(m.odds)}">${fmtOdds(m.odds)}</span> · Balance ${fmtChips(ME.chips)}${bonusBal ? ` · Bonus ${fmtChips(bonusBal)}` : ""}</div>
    <input id="bet-wager" class="input" type="number" min="0" placeholder="${bonusBal ? "Wager in chips (optional with bonus bets)" : "Wager (chips)"}">
    ${bonusBal ? `<input id="bet-bonus" class="input" type="number" min="0" max="${bonusBal}" value="0" placeholder="Bonus bets to stake (max ${fmtChips(bonusBal)})">` : ""}
    ${promoInfo.boosts.length ? `<select id="bet-boost" class="input">
      <option value="">No profit boost</option>
      ${promoInfo.boosts.map((b) => `<option value="${b.id}">${esc(b.label)}${b.max_wager ? ` (max ${fmtChips(b.max_wager)})` : ""}</option>`).join("")}
    </select>` : ""}
    <div class="modal-payout dim" id="bet-payout"></div>
    <div class="row-buttons">
      <button class="btn btn-primary" id="bet-go">Place Bet</button>
      <button class="btn btn-outline" id="bet-cancel">Cancel</button>
    </div>`);
  const wagerEl = $("#bet-wager", overlay);
  const bonusEl = $("#bet-bonus", overlay);
  const boostEl = $("#bet-boost", overlay);
  const recalc = () => {
    const w = Number(wagerEl.value) || 0;
    const bonus = bonusEl ? (Number(bonusEl.value) || 0) : 0;
    const stake = w + bonus;
    let payout = payoutForWager(stake, m.odds);
    let pct = 0;
    if (boostEl && boostEl.value) {
      const b = promoInfo.boosts.find((x) => String(x.id) === boostEl.value);
      if (b) pct = b.pct;
    }
    if (pct) payout = stake + Math.round((payout - stake) * (1 + pct / 100));
    payout = Math.min(payout, singlePayoutCap());
    const shown = payout - bonus;
    $("#bet-payout", overlay).textContent = stake
      ? `Win ${fmtChips(shown)} chips${pct ? ` (+${pct}% boost)` : ""}${bonus ? ` — winnings only, bonus stake not returned` : ""}`
      : "";
  };
  wagerEl.addEventListener("input", recalc);
  if (bonusEl) bonusEl.addEventListener("input", recalc);
  if (boostEl) boostEl.addEventListener("change", recalc);
  $("#bet-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#bet-go", overlay).addEventListener("click", async () => {
    const wager = Number(wagerEl.value) || 0;
    const bonusV = bonusEl ? (Number(bonusEl.value) || 0) : 0;
    if (wager < 1 && bonusV < 1) return toast("Enter a wager or apply bonus bets.", "error");
    const body = { market_id: marketId, wager };
    if (bonusV > 0) body.bonus_amount = bonusV;
    if (boostEl && boostEl.value) body.profit_boost_token_id = Number(boostEl.value);
    try {
      const r = await api("/bet", { method: "POST", body });
      toast(r.message);
      overlay.remove();
      await refreshMe();
    } catch (e) { toast(e.message, "error"); }
  });
}

function openFeatureParlayModal() {
  const overlay = modal(`
    <h3>Feature Parlay</h3>
    <div class="dim">Turns your current slip into a no-wager GM parlay on the tail board and clears your slip.</div>
    <input id="feature-name" class="input" type="text" maxlength="100" placeholder="Name (required)">
    <textarea id="feature-desc" class="input" maxlength="500" placeholder="Description (optional)"></textarea>
    <div class="row-buttons">
      <button class="btn btn-primary" id="feature-go">Feature</button>
      <button class="btn btn-outline" id="feature-cancel">Cancel</button>
    </div>`);
  $("#feature-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#feature-go", overlay).addEventListener("click", async () => {
    const name = $("#feature-name", overlay).value.trim();
    const description = $("#feature-desc", overlay).value.trim();
    if (!name) return toast("Enter a name for the featured parlay.", "error");
    try {
      const r = await api("/parlay/feature", { method: "POST", body: { name, description } });
      toast(r.message);
      overlay.remove();
      location.hash = "#tail";
    } catch (e) { toast(e.message, "error"); }
  });
}

function openWagerModal({ title, odds, onSubmit, after }) {
  const overlay = modal(`
    <h3>${esc(title)}</h3>
    <div class="dim">Balance ${fmtChips(ME.chips)}</div>
    <input id="w-wager" class="input" type="number" min="1" placeholder="Wager (chips)">
    <div class="modal-payout dim" id="w-payout"></div>
    <div class="row-buttons">
      <button class="btn btn-primary" id="w-go">Confirm</button>
      <button class="btn btn-outline" id="w-cancel">Cancel</button>
    </div>`);
  const wagerEl = $("#w-wager", overlay);
  if (odds != null) {
    wagerEl.addEventListener("input", () => {
      const w = Number(wagerEl.value) || 0;
      const payout = Math.min(payoutForWager(w, odds), parlayPayoutCap());
      $("#w-payout", overlay).textContent = w ? `Win ${fmtChips(payout)} chips` : "";
    });
  }
  $("#w-cancel", overlay).addEventListener("click", () => overlay.remove());
  $("#w-go", overlay).addEventListener("click", async () => {
    const wager = Number(wagerEl.value);
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
