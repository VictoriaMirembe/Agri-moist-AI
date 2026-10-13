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
