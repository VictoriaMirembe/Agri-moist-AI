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

function showError(el, err) {
  el.textContent = "Problem: " + (err.message || err);
  el.style.display = "block";
}
