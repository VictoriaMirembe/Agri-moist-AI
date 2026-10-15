// Small shared helpers for officer and farmer pages. Plain fetch, no framework,
// so the pages stay light on weak signal and need no build step.

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data.detail || res.statusText;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

async function getJSON(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data.detail || res.statusText;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

// Escape any database or user value before it goes into an HTML string, so text
// like "<script>" or "</textarea>" shows as text instead of becoming markup.
function escapeHTML(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Only http(s) links may become an href; anything else (e.g. "javascript:") is dropped.
function safeURL(value) {
  return /^https?:\/\//i.test(String(value || "")) ? escapeHTML(value) : "";
}

// Disable the given buttons while `action` runs, so a slow request cannot be sent
// twice. They stay disabled on success (the page reloads or re-renders) and are
// re-enabled if the request fails.
async function whileBusy(buttons, action) {
  buttons.forEach((b) => { b.disabled = true; });
  try {
    return await action();
  } catch (err) {
    buttons.forEach((b) => { b.disabled = false; });
    throw err;
  }
}

// Re-run `refresh` every `intervalMs` while the page is open, so officer screens
// show new farmer reports, flags and drafts without a manual reload. Polling stops
// while the tab is hidden and runs at once when it becomes visible again. A refresh
// never overlaps the previous one.
function autoRefresh(refresh, intervalMs = 30000) {
  let timer = null;
  let running = false;

  async function tick() {
    if (running || document.visibilityState === "hidden") return;
    running = true;
    try {
      await refresh();
    } catch (_) {
      // A failed background refresh keeps what is on screen; the next tick retries.
    } finally {
      running = false;
    }
  }

  function start() {
    if (timer === null) timer = setInterval(tick, intervalMs);
  }

  function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      stop();
    } else {
      tick();
      start();
    }
  });
  if (document.visibilityState !== "hidden") start();
  return tick;
}

// True while the officer is working in a card: unsaved text in a textarea, focus
// inside it, a request in progress (a disabled button) or an error still showing.
// Auto-refresh keeps such a card exactly as it is.
function cardInUse(card) {
  const editing = Array.from(card.querySelectorAll("textarea")).some((t) => t.value !== t.defaultValue);
  const focused = card.contains(document.activeElement);
  const busy = Array.from(card.querySelectorAll("button")).some((b) => b.disabled);
  const error = card.querySelector(".error");
  const errorShowing = Boolean(error && error.style.display !== "none");
  return editing || focused || busy || errorShowing;
}

// Put fresh cards (from `html`) into `listEl`, but keep every existing card that is
// in use (see cardInUse) in place of its fresh copy. A card in use that is no longer
// in the fresh list - for example decided by another officer - is kept at the top so
// the officer's text is never lost; acting on it then shows the server's answer.
function replaceCards(listEl, html, idAttr) {
  const kept = new Map();
  listEl.querySelectorAll(`[${idAttr}]`).forEach((card) => {
    if (cardInUse(card)) kept.set(card.getAttribute(idAttr), card);
  });
  if (!kept.size) {
    listEl.innerHTML = html;
    return;
  }
  const fresh = document.createElement("div");
  fresh.innerHTML = html;
  fresh.querySelectorAll(`[${idAttr}]`).forEach((card) => {
    const old = kept.get(card.getAttribute(idAttr));
    if (old) {
      card.replaceWith(old);
      kept.delete(card.getAttribute(idAttr));
    }
  });
  kept.forEach((old) => fresh.prepend(old));
  listEl.replaceChildren(...fresh.childNodes);
}

// Run an officer action from a card's buttons: the buttons are disabled while it
// runs (whileBusy) and an error is shown inside the card, never in an alert(). On
// success the card is released (buttons enabled, focus removed) so `reload` and
// the auto-refresh redraw it with its new state.
async function cardAction(card, request, reload) {
  const error = card.querySelector(".error");
  const buttons = Array.from(card.querySelectorAll(".actions button"));
  if (error) error.style.display = "none";
  try {
    await whileBusy(buttons, request);
  } catch (err) {
    if (error) showError(error, err);
    return;
  }
  buttons.forEach((b) => { b.disabled = false; });
  if (card.contains(document.activeElement)) document.activeElement.blur();
  await reload();
}

// Show a load error only if the list never loaded; a failed background refresh
// leaves the last good list on screen.
function showLoadError(listEl, err, prefix = "Could not load: ") {
  if (!listEl.dataset.loaded) listEl.textContent = prefix + err.message;
}

function showError(el, err) {
  el.textContent = "Problem: " + (err.message || err);
  el.style.display = "block";
}

// ---------------------------------------------------------------------------
// Plain words for officer screens. The stored values never change; these only
// decide what an officer reads (the same words as backend/officer/pages.py).
// ---------------------------------------------------------------------------
const STATUS_WORDS = {
  pending_review: "Waiting for approval",
  approved: "Approved",
  edited_and_approved: "Approved with edits",
  rejected: "Rejected",
  blocked: "Blocked: a fact has no source",
  drafted: "Draft ready for approval",
  flagged: "Sent for a visit instead",
  failed: "Did not finish",
  unchecked: "New",
  checked: "Checked",
  open: "Visit needed",
  visited: "Visited",
  closed: "Closed",
};
const CHANNEL_WORDS = {
  officer_visit: "Officer visit",
  farmer_ussd: "Farmer by phone (USSD)",
  farmer_web: "Farmer web page",
  assistant: "AI assistant",
  officer: "Officer",
};
const ANSWER_WORDS = { yes: "Yes", no: "No", partly: "Partly", too_early: "Too early to say" };

function plainStatus(value) {
  return STATUS_WORDS[value] || String(value || "").replace(/_/g, " ");
}

function plainChannel(value) {
  return CHANNEL_WORDS[value] || String(value || "-").replace(/_/g, " ");
}

function plainAnswer(value) {
  return value ? (ANSWER_WORDS[value] || String(value).replace(/_/g, " ")) : "Not answered";
}

function statusTone(value) {
  if (["approved", "edited_and_approved", "checked", "visited", "drafted"].includes(value)) return "green";
  if (["blocked", "rejected", "failed", "open"].includes(value)) return "red";
  return "amber";
}

// A status pill always carries its words - colour is never the only signal.
function statusPill(value) {
  return `<span class="pill ${statusTone(value)}">${escapeHTML(plainStatus(value))}</span>`;
}

// "12 Oct, 11:01" in Kampala time from an ISO timestamp; "12 Oct" for a date alone.
// Timestamps without a zone were stored in UTC.
function formatWhen(value) {
  if (!value) return "";
  const text = String(value);
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(text);
  const iso = dateOnly ? text + "T12:00:00Z" : (/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text : text + "Z");
  const d = new Date(iso);
  if (isNaN(d)) return text;
  const opts = dateOnly
    ? { day: "numeric", month: "short", timeZone: "Africa/Kampala" }
    : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Africa/Kampala" };
  try {
    return new Intl.DateTimeFormat("en-GB", opts).format(d);
  } catch (_) {
    return text.slice(0, 16).replace("T", " ");
  }
}

// Crop calendar days such as "03-15" as "15 Mar".
function formatMonthDay(value) {
  const m = /^(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!m) return String(value || "");
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(m[2])} ${months[Number(m[1]) - 1] || m[1]}`;
}

// Fact text written by the assistant uses stored words such as "farmer_ussd";
// show them in plain words. The saved fact is not changed.
function plainFact(text) {
  return String(text || "")
    .replace(/\bofficer_visit\b/g, "officer visit")
    .replace(/\bfarmer_ussd\b/g, "farmer by phone")
    .replace(/\bfarmer_web\b/g, "farmer web page")
    .replace(/\bunchecked\b/g, "not yet checked")
    .replace(/\bNone\b/g, "not recorded");
}

// "Group 3 · Iriiri" - pages that know the villages set window.GROUP_VILLAGES.
function groupName(id) {
  const village = (window.GROUP_VILLAGES || {})[String(id)];
  return "Group " + id + (village ? " · " + village : "");
}

// A farmer message (report) card, used on Approve advice and Farmer messages.
// `idAttr` is the page's card attribute ("data-id" or "data-report-id"); every value
// from the server is escaped.
function reportCard(r, idAttr) {
  const details = [
    r.crop ? ["Crop", r.crop] : null,
    r.pest_seen ? ["Pest seen", r.pest_seen] : null,
    r.rain_last_7_days ? ["Rain in the last 7 days", r.rain_last_7_days] : null,
    r.crop_condition ? ["Crop condition", r.crop_condition] : null,
  ].filter(Boolean).map(([k, v]) => `<div><dt>${k}</dt><dd>${escapeHTML(v)}</dd></div>`).join("");
  const group = r.group_id ? `<a href="/groups/${encodeURIComponent(r.group_id)}">${escapeHTML(groupName(r.group_id))}</a>` : "";
  return `
  <article class="card o-card" ${idAttr}="${Number(r.id)}">
    <div class="o-card-head">
      <p class="o-card-title"><strong>${escapeHTML(r.household_code)}</strong> · ${escapeHTML(plainChannel(r.source))}</p>
      ${statusPill(r.checked)}
    </div>
    <p class="o-card-meta">${escapeHTML(formatWhen(r.observed_on))}${group ? " · " + group : ""}</p>
    ${details ? `<dl class="o-facts">${details}</dl>` : ""}
    <p>${r.notes ? escapeHTML(r.notes) : "<span class='muted'>No extra notes.</span>"}</p>
    ${r.checked === "unchecked" ? `<div class="row actions"><button class="btn secondary" onclick="markChecked(${Number(r.id)})">Mark as checked</button></div>` : ""}
    <p class="error pill red alert alert-danger" style="display:none;margin-top:0.6rem;"></p>
  </article>`;
}

// A "visit needed" card, used on Approve advice and Visits needed.
function flagCard(f, idAttr) {
  return `
  <article class="card o-card" ${idAttr}="${Number(f.id)}">
    <div class="o-card-head">
      <p class="o-card-title"><a href="/groups/${encodeURIComponent(f.group_id)}">${escapeHTML(groupName(f.group_id))}</a></p>
      ${statusPill(f.status)}
    </div>
    <p class="o-card-meta">Asked by ${escapeHTML(plainChannel(f.raised_by))} · ${escapeHTML(formatWhen(f.created_at))}</p>
    <p>${escapeHTML(plainFact(f.reason))}</p>
    ${f.visited_by ? `<p class="muted">Visited - marked by ${escapeHTML(f.visited_by)}, ${escapeHTML(formatWhen(f.visited_at))}</p>` : ""}
    ${f.status === "open" ? `<div class="row actions"><button class="btn secondary" onclick="markVisited(${Number(f.id)})">Mark as visited</button></div>` : ""}
    <p class="error pill red alert alert-danger" style="display:none;margin-top:0.6rem;"></p>
  </article>`;
}
