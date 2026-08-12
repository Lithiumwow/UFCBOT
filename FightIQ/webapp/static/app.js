/* FightIQ web sandbox — full props */

const state = {
  mode: "parlay", // default multi-pick ticket builder
  events: [],
  card: null,
  fight: null,
  fightSlug: null,
  sportsbook: "",
  playView: "popular", // popular | all
  category: "",
  playQuery: "",
  plays: [],
  playMeta: null,
  legs: [],
  turnstileSiteKey: "0x4AAAAAADwy1EZal2MP6ASX",
  turnstileWidgetId: null,
};

const el = (id) => document.getElementById(id);

const MODE_HINTS = {
  straight:
    "Single moneyline focus. Each new click still stacks on the ticket unless you Clear — toggle a play off by clicking it again.",
  prop: "Props build. Click any market; ticket stacks picks and shows combined odds when 2+.",
  parlay:
    "Stack ML + props (same fight or across). Combined American odds update as you add legs.",
};

function legKey(leg) {
  if (leg.play_id && leg.fight_slug) return `${leg.fight_slug}::${leg.play_id}`;
  if (leg.play_id) return String(leg.play_id);
  return `${leg.fight_slug || ""}::${leg.label || leg.description || ""}::${leg.american}`;
}

function isLegSelected(playId, fightSlug) {
  const key = `${fightSlug || state.fightSlug}::${playId}`;
  return state.legs.some((l) => legKey(l) === key);
}

const CAT_LABELS = {
  moneyline: "Moneyline",
  totals: "Totals O/U",
  distance: "Distance / start",
  method_fight: "Fight method",
  method_fighter: "Fighter method",
  round_fighter: "Round winner",
  round_method: "Round + method",
  other: "Other",
};

const CAT_ORDER = [
  "moneyline",
  "totals",
  "distance",
  "method_fight",
  "method_fighter",
  "round_fighter",
  "round_method",
  "other",
];

function sortCategories(keys) {
  return [...keys].sort((a, b) => {
    const ia = CAT_ORDER.indexOf(a);
    const ib = CAT_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  // Betslip generate may return structured error body with 502 — still return data
  if (!res.ok) {
    if (data && (data.message || data.link || data.mode === "error")) {
      data._http_status = res.status;
      return data;
    }
    throw new Error(data.error || data.message || res.statusText || "Request failed");
  }
  return data;
}

function toast(msg, kind = "") {
  const t = el("toast");
  t.hidden = false;
  t.textContent = msg;
  t.className = "toast" + (kind ? " " + kind : "");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    t.hidden = true;
  }, 2800);
}

function setStatus(text, cls = "") {
  const s = el("status");
  s.textContent = text;
  s.className = "status" + (cls ? " " + cls : "");
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".mode").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  el("modeHint").textContent = MODE_HINTS[mode] || MODE_HINTS.parlay;
  // Modes only guide which markets to browse — ticket always multi-selects.
  // Optional: straight trims nothing (preserve user's stacked slip).
  renderTicket();
  syncPlaySelectionUi();
  if (state.fightSlug) loadPlays();
}

async function loadSportsbooks() {
  try {
    const books = await api("/api/sportsbooks");
    const sel = el("bookSelect");
    const cur = state.sportsbook;
    sel.innerHTML =
      `<option value="">All books (best price)</option>` +
      books
        .map(
          (b) =>
            `<option value="${escapeAttr(b.shortName)}">${escapeHtml(
              b.fullName || b.shortName
            )}</option>`
        )
        .join("");
    sel.value = cur;
  } catch (e) {
    /* optional */
  }
}

async function loadEvents() {
  setStatus("Loading events…");
  try {
    state.events = await api("/api/events?limit=30");
    renderEvents();
    setStatus(`Ready · ${state.events.length} events`, "ok");
  } catch (e) {
    setStatus(e.message, "err");
    toast(e.message, "err");
  }
}

function renderEvents() {
  const box = el("eventList");
  if (!state.events.length) {
    box.innerHTML = `<div class="muted">No upcoming events.</div>`;
    return;
  }
  box.innerHTML = state.events
    .map(
      (e) => `
    <button type="button" class="item" data-pk="${e.pk}" data-event-name="${escapeAttr(e.name)}">
      <span class="date">${e.date || "TBD"} · ${e.promotion || "—"}</span>
      <span class="name">${escapeHtml(e.name)}</span>
    </button>`
    )
    .join("");
}

function onEventPick(btn) {
  const box = el("eventList");
  box.querySelectorAll(".item").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  const eventName = btn.getAttribute("data-event-name") || "";
  const pk = btn.getAttribute("data-pk") || "";
  el("eventSearch").value = eventName;
  loadCard({ name: eventName, pk });
}

async function loadCard(query) {
  let name = "";
  let pk = "";
  if (query && typeof query === "object") {
    name = (query.name || "").trim();
    pk = (query.pk || "").trim();
  } else {
    name = (query || el("eventSearch").value || "").trim();
  }
  if (!name && !pk) return toast("Enter an event name", "err");
  el("cardMeta").textContent = "Loading…";
  el("fightList").innerHTML = "";
  try {
    const params = new URLSearchParams();
    if (pk) params.set("pk", pk);
    if (name) params.set("q", name);
    const data = await api(`/api/card?${params}`);
    state.card = data;
    el("cardMeta").textContent = `${data.event_date || ""} · ${data.count} fights`;
    renderFights(data.fights);
    setStatus(`Card: ${data.event_name}`, "ok");
  } catch (e) {
    el("cardMeta").textContent = "Failed";
    toast(e.message, "err");
    setStatus(e.message, "err");
  }
}

function renderFights(fights) {
  const box = el("fightList");
  box.innerHTML = fights
    .map((f) => {
      const ml = f.formatted.ml;
      return `
      <button class="fight-item" data-slug="${escapeAttr(f.slug)}">
        <div class="names">${escapeHtml(f.fighter1)} vs ${escapeHtml(f.fighter2)}</div>
        <div class="meta">
          <span>tap for props</span>
          <span class="ml">${ml[0]} / ${ml[1]}</span>
        </div>
      </button>`;
    })
    .join("");
  box.querySelectorAll(".fight-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      box.querySelectorAll(".fight-item").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      openFight(btn.dataset.slug);
    });
  });
}

async function openFight(slug) {
  state.fightSlug = slug;
  state.playQuery = "";
  el("playSearch").value = "";
  el("playToolbar").hidden = false;

  // Instant context from card cache — skip separate /api/fight round-trip
  const fromCard = (state.card && state.card.fights || []).find((f) => f.slug === slug);
  if (fromCard) {
    state.fight = fromCard;
    el("boardMeta").textContent =
      (fromCard.label || `${fromCard.fighter1} vs ${fromCard.fighter2}`) +
      " · " +
      (fromCard.event_name || state.card.event_name || "");
    // Instant moneyline from card while props stream in
    renderQuickMoneyline(fromCard);
  } else {
    el("boardMeta").textContent = "Loading props…";
    el("board").className = "plays-list empty";
    el("board").textContent = "Fetching sportsbook props…";
  }

  await loadPlays();
}

function renderQuickMoneyline(fight) {
  const f1 = fight.fighter1 || "Fighter 1";
  const f2 = fight.fighter2 || "Fighter 2";
  const mlFmt = (fight.formatted && fight.formatted.ml) || ["—", "—"];
  const mlOdd = (fight.odds && fight.odds.ml) || [null, null];
  const label = fight.label || `${f1} vs ${f2}`;

  el("board").className = "plays-list";
  el("board").innerHTML = `
    <div class="play-group">
      <h3>Moneyline <span class="muted" style="font-weight:400;font-size:0.8rem">(props loading…)</span></h3>
      <div class="play-rows">
        <button type="button" class="play-btn ml-quick" data-side="1"
          data-fighter="${escapeAttr(f1)}" data-american="${escapeAttr(mlOdd[0] ?? "")}"
          data-label="${escapeAttr(label)}" ${mlOdd[0] == null ? "disabled" : ""}>
          <span class="play-label">${escapeHtml(f1)}</span>
          <span class="play-right"><span class="price">${escapeHtml(mlFmt[0])}</span></span>
        </button>
        <button type="button" class="play-btn ml-quick" data-side="2"
          data-fighter="${escapeAttr(f2)}" data-american="${escapeAttr(mlOdd[1] ?? "")}"
          data-label="${escapeAttr(label)}" ${mlOdd[1] == null ? "disabled" : ""}>
          <span class="play-label">${escapeHtml(f2)}</span>
          <span class="play-right"><span class="price">${escapeHtml(mlFmt[1])}</span></span>
        </button>
      </div>
    </div>`;

  el("board").querySelectorAll(".ml-quick:not(:disabled)").forEach((btn) => {
    // Highlight if already on ticket
    const pid = `ml-card:${state.fightSlug}:${btn.dataset.side}`;
    if (state.legs.some((l) => l.play_id === pid)) btn.classList.add("selected");
    btn.addEventListener("click", () => toggleCardMl(btn));
  });
}

function toggleCardMl(btn) {
  const side = btn.dataset.side;
  const fighter = btn.dataset.fighter;
  const label = btn.dataset.label || "";
  const american = Number(btn.dataset.american);
  if (!Number.isFinite(american)) return toast("No moneyline price", "err");

  const playId = `ml-card:${state.fightSlug}:${side}`;
  const key = `${state.fightSlug}::${playId}`;
  const existingIdx = state.legs.findIndex((l) => legKey(l) === key);
  if (existingIdx >= 0) {
    state.legs.splice(existingIdx, 1);
    renderTicket();
    btn.classList.remove("selected");
    toast("Removed from ticket", "ok");
    return;
  }

  const fmt =
    american > 0 ? `+${american}` : String(american);
  state.legs.push({
    description: `${fighter} (${label})`,
    fight: label,
    fight_slug: state.fightSlug,
    play_id: playId,
    label: fighter,
    market: "STRAIGHT",
    american,
    formatted: fmt,
    source: "card_ml",
  });
  if (state.legs.length >= 2 && state.mode !== "parlay") {
    state.mode = "parlay";
    document.querySelectorAll(".mode").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === "parlay");
    });
  }
  btn.classList.add("selected");
  renderTicket();
  toast(`Added ${fighter} ${fmt}`, "ok");
}

function formatAmericanLocal(a) {
  if (a == null || Number.isNaN(Number(a))) return "—";
  const n = Number(a);
  return n > 0 ? `+${n}` : String(n);
}

async function loadPlays() {
  if (!state.fightSlug) return;
  const params = new URLSearchParams();
  if (state.sportsbook) params.set("sportsbook", state.sportsbook);
  if (state.playQuery) {
    params.set("q", state.playQuery);
    params.set("all", "1");
  } else if (state.playView === "all") {
    params.set("all", "1");
    params.set("popular", "0");
  } else {
    params.set("popular", "1");
  }
  if (state.category) params.set("category", state.category);
  params.set("limit", "120");

  setStatus("Loading plays…");
  try {
    const data = await api(
      `/api/fight/${encodeURIComponent(state.fightSlug)}/plays?${params}`
    );
    state.playMeta = data;
    state.plays = data.plays || [];
    // Merge sportsbooks from catalog into select if richer
    if (data.sportsbooks && data.sportsbooks.length) {
      mergeBooks(data.sportsbooks);
    }
    renderCategoryChips(data.categories || {});
    renderPlays();
    const scope = data.popular_only
      ? "popular"
      : state.playQuery
      ? `search “${data.query}”`
      : "all";
    el("playCount").textContent = `Showing ${state.plays.length} of ${data.total} matches · ${data.total_all} total playables on file · ${scope}`;
    setStatus(`Plays ready · ${data.total_all} in catalog`, "ok");
  } catch (e) {
    toast(e.message, "err");
    setStatus(e.message, "err");
    el("board").textContent = e.message;
  }
}

function mergeBooks(books) {
  const sel = el("bookSelect");
  const current = sel.value;
  const have = new Set([...sel.options].map((o) => o.value));
  books.forEach((b) => {
    if (!have.has(b.shortName)) {
      const opt = document.createElement("option");
      opt.value = b.shortName;
      opt.textContent = b.fullName || b.shortName;
      sel.appendChild(opt);
    }
  });
  sel.value = current;
}

function renderCategoryChips(cats) {
  const box = el("catChips");
  const keys = sortCategories(Object.keys(cats));
  box.innerHTML =
    `<button class="chip ${!state.category ? "active" : ""}" data-cat="">All cats</button>` +
    keys
      .map(
        (k) =>
          `<button class="chip ${state.category === k ? "active" : ""}" data-cat="${escapeAttr(
            k
          )}">${escapeHtml(CAT_LABELS[k] || k)} (${cats[k]})</button>`
      )
      .join("");
  box.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.category = btn.dataset.cat || "";
      loadPlays();
    });
  });
}

function renderPlays() {
  const board = el("board");
  if (!state.plays.length) {
    board.className = "plays-list empty";
    board.textContent =
      "No plays for this filter. Try All, clear the bookie, or search e.g. “submission”.";
    return;
  }
  board.className = "plays-list";
  // group by category — moneyline group always first
  const groups = {};
  state.plays.forEach((p) => {
    (groups[p.category] = groups[p.category] || []).push(p);
  });
  // moneyline: ensure STRAIGHT sides listed fighter1 then fighter2 (side 1, 2)
  if (groups.moneyline) {
    groups.moneyline.sort(
      (a, b) => (a.side || 0) - (b.side || 0) || a.label.localeCompare(b.label)
    );
  }
  board.innerHTML = sortCategories(Object.keys(groups))
    .map((cat) => {
      const items = groups[cat]
        .map((p) => {
          const disabled = p.american == null;
          const books =
            p.book_count > 0
              ? `<span class="book-count">${p.book_count} book${p.book_count > 1 ? "s" : ""}</span>`
              : "";
          const selected = isLegSelected(p.id, state.fightSlug);
          return `
          <button class="play-btn ${selected ? "selected" : ""}" data-id="${escapeAttr(
            p.id
          )}" ${disabled ? "disabled" : ""}>
            <span class="play-label">${escapeHtml(p.label)}</span>
            <span class="play-right">
              ${books}
              <span class="price ${disabled ? "na" : ""}">${p.formatted}</span>
            </span>
          </button>`;
        })
        .join("");
      return `
        <div class="play-group">
          <h3>${escapeHtml(CAT_LABELS[cat] || cat)}</h3>
          <div class="play-rows">${items}</div>
        </div>`;
    })
    .join("");

  board.querySelectorAll(".play-btn:not(:disabled)").forEach((btn) => {
    btn.addEventListener("click", () => togglePlay(btn.dataset.id));
  });
}

function syncPlaySelectionUi() {
  document.querySelectorAll(".play-btn[data-id]").forEach((btn) => {
    btn.classList.toggle(
      "selected",
      isLegSelected(btn.dataset.id, state.fightSlug)
    );
  });
}

async function togglePlay(playId) {
  if (!state.fightSlug) return toast("Pick a fight first", "err");

  // Toggle off if already on ticket
  const key = `${state.fightSlug}::${playId}`;
  const existingIdx = state.legs.findIndex((l) => legKey(l) === key);
  if (existingIdx >= 0) {
    state.legs.splice(existingIdx, 1);
    await renderTicket();
    syncPlaySelectionUi();
    toast("Removed from ticket", "ok");
    setStatus("Ready", "ok");
    return;
  }

  try {
    setStatus("Pricing…");
    const body = {
      fight_slug: state.fightSlug,
      play_id: playId,
    };
    if (state.sportsbook) body.sportsbook = state.sportsbook;
    const leg = await api("/api/price", {
      method: "POST",
      body: JSON.stringify(body),
    });
    // Always append — build multi-leg slip automatically
    state.legs.push(leg);
    // Visual mode: once 2+ legs, treat as parlay
    if (state.legs.length >= 2 && state.mode !== "parlay") {
      state.mode = "parlay";
      document.querySelectorAll(".mode").forEach((btn) => {
        btn.classList.toggle("active", btn.dataset.mode === "parlay");
      });
      el("modeHint").textContent = MODE_HINTS.parlay;
    }
    await renderTicket();
    syncPlaySelectionUi();
    const n = state.legs.length;
    toast(
      n === 1 ? `Leg 1 · ${leg.formatted}` : `${n} legs · combo updated`,
      "ok"
    );
    setStatus("Ready", "ok");
  } catch (e) {
    toast(e.message, "err");
    setStatus(e.message, "err");
  }
}

async function renderTicket() {
  const box = el("ticketLegs");
  const copyBtn = el("copyTicket");
  const qpBtn = el("genQuickpick");
  const gbBtn = el("genGambly");
  const pbBtn = el("genPlaybook");
  const msg = el("betslipMsg");
  const labelEl = el("combinedLabel");
  const empty = !state.legs.length;

  if (empty) {
    box.className = "ticket-legs empty";
    box.textContent = "No legs yet. Click plays to build a slip.";
    el("combinedPrice").textContent = "—";
    el("combinedSub").textContent = "";
    if (labelEl) labelEl.textContent = "Combined";
    copyBtn.disabled = true;
    [qpBtn, gbBtn, pbBtn].forEach((b) => {
      if (b) b.disabled = true;
    });
    if (msg) {
      msg.value = "";
      msg.disabled = true;
    }
    hideBetslipOut();
    syncPlaySelectionUi();
    return;
  }
  box.className = "ticket-legs";
  box.innerHTML = state.legs
    .map(
      (leg, i) => `
    <div class="leg">
      <button class="remove" data-i="${i}" title="Remove">×</button>
      <div class="leg-num">${i + 1}</div>
      <div class="desc">${escapeHtml(leg.description || leg.label)}</div>
      <div class="odds">${leg.formatted}${
        leg.sportsbook ? ` · ${escapeHtml(leg.sportsbook)}` : ""
      }</div>
    </div>`
    )
    .join("");
  box.querySelectorAll(".remove").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.legs.splice(Number(btn.dataset.i), 1);
      renderTicket();
    });
  });
  copyBtn.disabled = false;
  [qpBtn, gbBtn, pbBtn].forEach((b) => {
    if (b) b.disabled = false;
  });
  if (msg) {
    msg.disabled = false;
    // Keep manual edits unless empty / still default
    if (!msg.dataset.touched || msg.value.trim() === "") {
      msg.value = ticketBotText();
      msg.dataset.touched = "";
    }
  }
  syncPlaySelectionUi();

  try {
    const combo = await api("/api/combine", {
      method: "POST",
      body: JSON.stringify({ legs: state.legs }),
    });
    el("combinedPrice").textContent = combo.formatted;
    if (state.legs.length === 1) {
      if (labelEl) labelEl.textContent = "Straight";
      el("combinedSub").textContent = `Decimal ${combo.combined_decimal} · click another play to parlay`;
    } else {
      if (labelEl) labelEl.textContent = `${state.legs.length}-leg parlay`;
      const parts = state.legs.map((l) => l.formatted).join(" × ");
      el("combinedSub").textContent = `${parts} → ${combo.formatted} · ~${(
        combo.implied_prob * 100
      ).toFixed(1)}%`;
    }
    // refresh draft message when legs/combo change (unless user is typing)
    if (msg && !msg.dataset.touched) {
      msg.value = ticketBotText();
    }
  } catch (e) {
    el("combinedPrice").textContent = "—";
    el("combinedSub").textContent = e.message;
  }
}

function ticketSummaryText() {
  const kind =
    state.legs.length >= 2 ? `${state.legs.length}-LEG PARLAY` : "STRAIGHT";
  const lines = [
    `FightIQ ${kind}`,
    state.sportsbook ? `Book: ${state.sportsbook}` : "Book: best available",
    "",
  ];
  state.legs.forEach((l, i) => {
    // Prefer description: "Fighter (F1 vs F2) @ -150"
    const desc = l.description || l.label || "leg";
    lines.push(`${i + 1}. ${desc} @ ${l.formatted}`);
  });
  const price = el("combinedPrice").textContent;
  const sub = el("combinedSub").textContent;
  lines.push("", `Combined: ${price}`);
  if (sub) lines.push(sub);
  return lines.join("\n");
}

/** Compact lines for generators — same leg shape as copy summary */
function ticketBotText() {
  const custom = el("betslipMsg");
  if (custom && custom.dataset.touched === "1" && custom.value.trim()) {
    return custom.value.trim();
  }
  return state.legs
    .map((l, i) => {
      const desc = l.description || l.label || "leg";
      return `${i + 1}. ${desc} @ ${l.formatted || ""}`.trim();
    })
    .join("\n");
}

function hideBetslipOut() {
  const out = el("betslipOut");
  if (!out) return;
  out.hidden = true;
  out.innerHTML = "";
  out.className = "betslip-out muted";
}

function showBetslipOut(html, kind = "") {
  const out = el("betslipOut");
  if (!out) return;
  out.hidden = false;
  out.className = "betslip-out" + (kind ? " " + kind : "");
  out.innerHTML = html;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    el("ticketRaw").hidden = false;
    el("ticketRaw").textContent = text;
    return false;
  }
}

async function generateBetslip(provider) {
  if (!state.legs.length) return toast("Add legs first", "err");
  const botText = (el("betslipMsg")?.value || ticketBotText()).trim();
  if (!botText) return toast("Message field empty", "err");

  // QuickPick uses create → poll (same as quickpick.pikkit.com network tab)
  if (provider === "quickpick") {
    return generateQuickpick(botText);
  }

  const btnMap = {
    gambly: "genGambly",
    playbook: "genPlaybook",
    quickpick: "genQuickpick",
  };
  const labels = {
    gambly: "Gambly",
    playbook: "Playbook",
    quickpick: "QuickPick",
  };
  const btnId = btnMap[provider] || "genQuickpick";
  const btn = el(btnId);
  const allBtns = ["genQuickpick", "genGambly", "genPlaybook"].map(el);
  const prev = btn ? btn.textContent : "";
  allBtns.forEach((b) => {
    if (b) {
      b.disabled = true;
      b.classList.add("busy");
    }
  });
  if (btn) btn.textContent = "…";

  const name = labels[provider] || provider;
  setStatus(`Waiting on ${name}…`);
  showBetslipOut(
    `<span class="spin"></span><strong>Sending to ${escapeHtml(name)}</strong>
     <div class="muted" style="margin-top:0.4rem">Stay here — results show in this panel.</div>
     <pre class="ticket-raw-inline">${escapeHtml(botText)}</pre>`,
    ""
  );

  try {
    const data = await api("/api/betslip/generate", {
      method: "POST",
      body: JSON.stringify({
        provider,
        text: botText,
        legs: state.legs,
        combined: el("combinedPrice").textContent,
      }),
    });

    if (data.link) {
      return showBetslipLink(name, data.link, data.message);
    }

    showBetslipOut(
      `<div><strong>${escapeHtml(name)}</strong> · no auto link</div>
       <div class="muted" style="margin-top:0.35rem">${escapeHtml(
         data.message || data.error || "No betslip link."
       )}</div>
       <pre class="ticket-raw-inline">${escapeHtml(data.text || botText)}</pre>`,
      "err"
    );
    toast(`${name}: no link yet`, "err");
    setStatus(`${name} needs setup`, "err");
  } catch (e) {
    showBetslipOut(
      `<div><strong>${escapeHtml(name)}</strong> failed</div>
       <div style="margin-top:0.35rem">${escapeHtml(e.message)}</div>`,
      "err"
    );
    toast(e.message, "err");
    setStatus(e.message, "err");
  } finally {
    allBtns.forEach((b) => {
      if (b) {
        b.disabled = !state.legs.length;
        b.classList.remove("busy");
      }
    });
    if (btn) btn.textContent = prev || name;
  }
}

function showBetslipLink(name, link, message) {
  showBetslipOut(
    `<div><strong>${escapeHtml(name)}</strong> · ready</div>
     ${message ? `<div class="muted" style="margin-top:0.25rem">${escapeHtml(message)}</div>` : ""}
     <a class="result-link" href="${escapeAttr(link)}" target="_blank" rel="noopener">${escapeHtml(
       link
     )}</a>
     <div class="result-actions">
       <button type="button" class="ghost" id="copyBsLink">Copy link</button>
       <button type="button" class="ghost" id="openBsLink">Open link</button>
     </div>
     <div class="muted" style="margin-top:0.4rem">Result stays in FightIQ — no auto redirect.</div>`,
    "ok"
  );
  el("copyBsLink")?.addEventListener("click", async () => {
    await copyText(link);
    toast("Link copied", "ok");
  });
  el("openBsLink")?.addEventListener("click", () => {
    window.open(link, "_blank", "noopener");
  });
  toast(`${name} slip ready`, "ok");
  setStatus(`${name} ready`, "ok");
}

/** Cloudflare Turnstile token (same sitekey QuickPick site uses). */
function ensureTurnstileWidget() {
  return new Promise((resolve, reject) => {
    const key = state.turnstileSiteKey;
    if (!key) return reject(new Error("No Turnstile site key"));

    const tryRender = () => {
      if (!window.turnstile) return false;
      try {
        if (state.turnstileWidgetId == null) {
          const host = el("cf-turnstile");
          if (!host) {
            reject(new Error("Turnstile host missing"));
            return true;
          }
          state.turnstileWidgetId = window.turnstile.render(host, {
            sitekey: key,
            size: "invisible",
            execution: "execute",
            appearance: "execute",
            callback: () => {},
            "error-callback": () => {},
          });
        }
        return true;
      } catch (e) {
        reject(e);
        return true;
      }
    };

    if (tryRender()) {
      if (state.turnstileWidgetId != null) resolve(state.turnstileWidgetId);
      return;
    }
    let n = 0;
    const t = setInterval(() => {
      n += 1;
      if (tryRender()) {
        clearInterval(t);
        if (state.turnstileWidgetId != null) resolve(state.turnstileWidgetId);
      } else if (n > 50) {
        clearInterval(t);
        reject(new Error("Turnstile script failed to load"));
      }
    }, 100);
  });
}

function getTurnstileToken() {
  return new Promise(async (resolve, reject) => {
    try {
      await ensureTurnstileWidget();
      const wid = state.turnstileWidgetId;
      const timeout = setTimeout(() => {
        reject(new Error("Turnstile verification timed out — refresh and try again"));
      }, 20000);
      window.turnstile.reset(wid);
      window.turnstile.execute(wid, {
        callback: (token) => {
          clearTimeout(timeout);
          resolve(token);
        },
        "error-callback": () => {
          clearTimeout(timeout);
          reject(
            new Error(
              "Turnstile failed (domain may need to be allowed for the sitekey). Try again."
            )
          );
        },
      });
    } catch (e) {
      reject(e);
    }
  });
}

async function generateQuickpick(botText) {
  const btn = el("genQuickpick");
  const allBtns = ["genQuickpick", "genGambly", "genPlaybook"].map(el);
  const prev = btn ? btn.textContent : "QuickPick";
  allBtns.forEach((b) => {
    if (b) {
      b.disabled = true;
      b.classList.add("busy");
    }
  });
  if (btn) btn.textContent = "…";

  const setBusyMsg = (html) => {
    showBetslipOut(
      `<span class="spin"></span>${html}
       <pre class="ticket-raw-inline">${escapeHtml(botText)}</pre>`,
      ""
    );
  };

  try {
    setBusyMsg("<strong>QuickPick</strong> · verifying…");
    setStatus("QuickPick: Turnstile…");
    let token = "";
    try {
      token = await getTurnstileToken();
    } catch (e) {
      // Continue without token only if external API might be configured
      console.warn("Turnstile:", e);
      showBetslipOut(
        `<div><strong>QuickPick</strong></div>
         <div class="muted" style="margin-top:0.35rem">${escapeHtml(e.message)}</div>
         <div class="muted">Retrying create; if this keeps failing the host domain isn’t allowed on Pikkit’s Turnstile key.</div>`,
        "err"
      );
    }

    setBusyMsg("<strong>QuickPick</strong> · create…");
    setStatus("QuickPick: create…");
    const created = await api("/api/betslip/quickpick/create", {
      method: "POST",
      body: JSON.stringify({
        text: botText,
        legs: state.legs,
        combined: el("combinedPrice").textContent,
        turnstile_token: token,
      }),
    });

    // External API path may complete immediately
    if (created.done && created.link) {
      return showBetslipLink("QuickPick", created.link, created.message);
    }
    if (created.link && created.status === "complete") {
      return showBetslipLink("QuickPick", created.link, created.message);
    }

    const rid = created.request_id;
    if (!rid) {
      showBetslipOut(
        `<div><strong>QuickPick</strong> · create failed</div>
         <div style="margin-top:0.35rem">${escapeHtml(
           created.message || created.error || "No request_id"
         )}</div>`,
        "err"
      );
      toast("QuickPick create failed", "err");
      setStatus("QuickPick failed", "err");
      return;
    }

    // Poll — GET /betslip/bot/website/get?request_id=…
    const maxPolls = 45;
    for (let i = 1; i <= maxPolls; i++) {
      setBusyMsg(
        `<strong>QuickPick</strong> · waiting for result… (${i}/${maxPolls})
         <div class="muted" style="margin-top:0.3rem">request_id ${escapeHtml(rid)}</div>`
      );
      setStatus(`QuickPick polling ${i}/${maxPolls}`);
      await sleep(1500);
      const st = await api(
        `/api/betslip/quickpick/status?request_id=${encodeURIComponent(rid)}`
      );
      if (st.complete && st.link) {
        return showBetslipLink("QuickPick", st.link, st.message);
      }
      if (st.status === "error" || st.status === "failed") {
        showBetslipOut(
          `<div><strong>QuickPick</strong> failed</div>
           <div style="margin-top:0.35rem">${escapeHtml(
             st.message || st.status
           )}</div>`,
          "err"
        );
        toast("QuickPick failed", "err");
        setStatus("QuickPick failed", "err");
        return;
      }
    }
    showBetslipOut(
      `<div><strong>QuickPick</strong> timed out</div>
       <div class="muted" style="margin-top:0.35rem">request_id ${escapeHtml(rid)} — try again</div>`,
      "err"
    );
    setStatus("QuickPick timeout", "err");
  } catch (e) {
    showBetslipOut(
      `<div><strong>QuickPick</strong> failed</div>
       <div style="margin-top:0.35rem">${escapeHtml(e.message)}</div>`,
      "err"
    );
    toast(e.message, "err");
    setStatus(e.message, "err");
  } finally {
    allBtns.forEach((b) => {
      if (b) {
        b.disabled = !state.legs.length;
        b.classList.remove("busy");
      }
    });
    if (btn) btn.textContent = prev;
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/'/g, "&#39;");
}

function wire() {
  document.querySelectorAll(".mode").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });
  el("reloadEvents").addEventListener("click", loadEvents);
  el("eventList").addEventListener("click", (e) => {
    const btn = e.target.closest(".item[data-pk]");
    if (btn) onEventPick(btn);
  });
  el("loadCardBtn").addEventListener("click", () => loadCard());
  el("eventSearch").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadCard();
  });
  el("clearTicket").addEventListener("click", () => {
    state.legs = [];
    const msg = el("betslipMsg");
    if (msg) {
      msg.value = "";
      msg.dataset.touched = "";
    }
    renderTicket();
    syncPlaySelectionUi();
  });
  el("copyTicket").addEventListener("click", async () => {
    const text = ticketSummaryText();
    const ok = await copyText(text);
    toast(ok ? "Copied ticket" : "Select text below to copy", "ok");
  });
  el("genQuickpick").addEventListener("click", () => generateBetslip("quickpick"));
  el("genGambly").addEventListener("click", () => generateBetslip("gambly"));
  el("genPlaybook").addEventListener("click", () => generateBetslip("playbook"));
  const msg = el("betslipMsg");
  if (msg) {
    msg.addEventListener("input", () => {
      msg.dataset.touched = "1";
    });
  }
  el("bookSelect").addEventListener("change", () => {
    state.sportsbook = el("bookSelect").value;
    if (state.fightSlug) loadPlays();
  });
  el("clearBook").addEventListener("click", () => {
    el("bookSelect").value = "";
    state.sportsbook = "";
    if (state.fightSlug) loadPlays();
  });
  el("searchPlaysBtn").addEventListener("click", () => {
    state.playQuery = el("playSearch").value.trim();
    loadPlays();
  });
  el("playSearch").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      state.playQuery = el("playSearch").value.trim();
      loadPlays();
    }
  });
  el("viewChips").querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      el("viewChips").querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      btn.classList.add("active");
      state.playView = btn.dataset.view;
      state.playQuery = "";
      el("playSearch").value = "";
      loadPlays();
    });
  });
}

async function boot() {
  wire();
  setMode("parlay");
  try {
    const [health, providers] = await Promise.all([
      api("/api/health"),
      api("/api/betslip/providers").catch(() => null),
      loadSportsbooks().catch(() => null),
      loadEvents(),
    ]);
    if (providers?.quickpick?.turnstile_site_key) {
      state.turnstileSiteKey = providers.quickpick.turnstile_site_key;
    }
    if (providers) {
      if (providers.quickpick?.website || providers.quickpick?.configured) {
        el("genQuickpick").title =
          "Send ticket → QuickPick create → poll until link";
      }
      if (providers.gambly?.configured) {
        el("genGambly").title = "Generate betslip via Gambly/Unabated API";
      }
    }
    // warm turnstile (non-blocking)
    ensureTurnstileWidget().catch(() => {});
    if (health && health.ok) setStatus("Ready", "ok");
  } catch (e) {
    setStatus(e.message, "err");
  }
}

boot();
