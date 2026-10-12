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

function showError(el, err) {
  el.textContent = "Problem: " + (err.message || err);
  el.style.display = "block";
}
