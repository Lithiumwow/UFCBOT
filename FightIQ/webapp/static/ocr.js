/* FightIQ OCR window — text parse for now; image upload later */

const el = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText || "Request failed");
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

async function parseSlip() {
  const text = el("slipText").value.trim();
  if (!text) return toast("Paste slip text first", "err");
  el("slipParseOut").textContent = "Working…";
  try {
    const data = await api("/api/slips/parse", {
      method: "POST",
      body: JSON.stringify({
        text,
        use_quickpick: el("useQuickpick").checked,
        save: true,
      }),
    });
    el("slipParseOut").textContent = [
      `Source: ${data.source}${data.quickpick_link ? " · " + data.quickpick_link : ""}`,
      data.slip_id ? `Saved id: ${data.slip_id}` : "Not saved",
      `${data.leg_count || 0} leg(s)`,
      ...(data.notes || []),
      "",
      ...(data.legs || []).map(
        (l, i) =>
          `${i + 1}. [${l.market}] ${l.selection || l.label} ${l.formatted || ""}`.trim()
      ),
    ].join("\n");
    el("slipParseOut").classList.remove("muted");
    toast(`Parsed ${data.leg_count || 0} leg(s)`, "ok");
    await loadSlips();
  } catch (e) {
    el("slipParseOut").textContent = e.message;
    toast(e.message, "err");
  }
}

async function loadSlips() {
  try {
    const status = await api("/api/slips/status");
    el("slipStatusMeta").textContent = status.quickpick_configured
      ? `QuickPick ready · ${status.open_count} open`
      : `Local parse · ${status.open_count} open (set QUICKPICK_API_KEY for Pikkit)`;
    const slips = await api("/api/slips?limit=20");
    const box = el("slipList");
    if (!slips.length) {
      box.innerHTML = `<div class="muted">No saved slips yet.</div>`;
      return;
    }
    box.innerHTML = slips
      .map((s) => {
        const st = s.status || "open";
        const legs = (s.legs || [])
          .map((l) => `${l.selection || l.label} [${l.status}]`)
          .join(" · ");
        return `
        <button class="slip-card" data-id="${escapeAttr(s.id)}">
          <div class="top">
            <span>${new Date((s.created_at || 0) * 1000).toLocaleString()}</span>
            <span class="status-${escapeAttr(st)}">${escapeHtml(st)}</span>
          </div>
          <div class="legs">${escapeHtml(legs || s.id)}</div>
        </button>`;
      })
      .join("");
    box.querySelectorAll(".slip-card").forEach((btn) => {
      btn.addEventListener("click", () => gradeOne(btn.dataset.id));
    });
  } catch (e) {
    el("slipList").textContent = e.message;
  }
}

async function gradeOne(id) {
  try {
    const r = await api(`/api/slips/${encodeURIComponent(id)}/grade`, {
      method: "POST",
      body: "{}",
    });
    toast(`Slip ${id}: ${r.status}`, r.status === "won" ? "ok" : "");
    await loadSlips();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function gradeOpen() {
  try {
    const r = await api("/api/slips/grade-open", { method: "POST", body: "{}" });
    toast(`Graded ${r.graded} slip(s)`, "ok");
    await loadSlips();
  } catch (e) {
    toast(e.message, "err");
  }
}

el("parseSlipBtn").addEventListener("click", parseSlip);
el("gradeOpenBtn").addEventListener("click", gradeOpen);
el("reloadSlips").addEventListener("click", loadSlips);
loadSlips();
