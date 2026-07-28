// Every call to the DedDrop HTTP API.

// The server injects this into the page it serves. A cross-origin page cannot
// read our HTML without CORS, so the token also blocks CSRF on the control
// endpoints.
const CONTROL_TOKEN =
  document.querySelector('meta[name="deddrop-control-token"]')?.content ?? '';

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function controlFetch(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { 'X-Control-Token': CONTROL_TOKEN, ...(options.headers ?? {}) },
  });
}

export const getStatus = () => getJSON('/api/status');
export const getUserStats = () => getJSON('/api/user-stats');
export const getSnapshots = () => getJSON('/api/snapshots');

export async function getTables() {
  const [aircraft, mesh] = await Promise.all([
    getJSON('/api/aircraft'),
    getJSON('/api/mesh-nodes'),
  ]);
  return { aircraft, mesh };
}

export async function getMeshMapperLink() {
  const res = await controlFetch('/api/meshmapper-link');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()).meshmapper_link;
}

// Returns the server's message whether it accepted or rejected the request, so
// the caller can surface a 401 rather than silently doing nothing.
async function trigger(path, fallback) {
  const res = await controlFetch(path, { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, message: res.ok ? (data.message ?? fallback) : (data.error ?? 'Rejected') };
}

export const triggerPoll = () => trigger('/api/trigger-poll', 'Poll triggered!');
export const triggerFlush = () => trigger('/api/trigger-flush', 'Flush & Dispatch triggered!');
