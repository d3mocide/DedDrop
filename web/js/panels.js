// The status badge, stat cards, profile banner, and snapshot archive list.

import * as api from './api.js';
import { esc, fmtNum } from './format.js';
import * as table from './table.js';

const $ = (id) => document.getElementById(id);
const setText = (id, value) => { $(id).innerText = value; };

export async function refreshStatus() {
  let data;
  try {
    data = await api.getStatus();
  } catch {
    setText('status-text', 'Disconnected');
    $('status-pulse').style.backgroundColor = 'var(--accent-rose)';
    return;
  }

  setText('status-text', data.dry_run ? 'Dry Run Mode' : 'Online & Active');
  $('service-status').style.borderColor =
    data.dry_run ? 'rgba(245, 158, 11, 0.4)' : 'rgba(16, 185, 129, 0.4)';
  $('status-pulse').style.backgroundColor =
    data.dry_run ? 'var(--accent-amber)' : 'var(--accent-emerald)';
  $('dry-run-badge').innerText = data.dry_run ? 'DRY RUN' : 'LIVE';

  setText('acc-count', fmtNum(data.accumulator_count || 0));
  setText('mesh-acc-count', fmtNum(data.mesh_accumulator_count || 0));
  setText('tab-ac-count', fmtNum(data.accumulator_count || 0));
  setText('tab-mesh-count', fmtNum(data.mesh_accumulator_count || 0));
  setText('ingested-pings-label', fmtNum(data.ingested_pings_count || 0));
  setText('poll-count-badge', `${data.poll_count || 0} Polls`);

  const elapsed = data.elapsed_hours || 0;
  const total = data.upload_interval_hours || 6;
  const pct = Math.min(100, (elapsed / total) * 100);
  $('window-progress').style.width = `${pct.toFixed(1)}%`;
  setText('window-timer', `${elapsed.toFixed(2)}h / ${total}h (${pct.toFixed(0)}%)`);

  // A retained window means the last dispatch failed; make that visible rather
  // than letting it look like nothing happened.
  const retry = $('retry-notice');
  if (data.retry_pending_in > 0) {
    retry.classList.add('visible');
    // innerText would print the markup, and the message is built from a number
    // this module computed, so there is nothing untrusted to escape here.
    retry.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-alert"/></svg>` +
                      `<span>Last dispatch failed — window retained, retrying in ` +
                      `${Math.ceil(data.retry_pending_in / 60)} min</span>`;
  } else {
    retry.classList.remove('visible');
  }

  const up = data.last_upload;
  if (up && Object.keys(up).length) {
    setText('last-ac-info',
            `${fmtNum(up.aircraft_count || 0)} sent / ${fmtNum(up.aircraft_imported || 0)} new`);
    setText('last-mesh-info',
            `${fmtNum(up.mesh_count || 0)} sent / ${fmtNum(up.mesh_imported || 0)} new`);
    setText('last-upload-time', dispatchedAt(up));
  }
}

// The summary survives restarts, so a dispatch from a previous day needs its
// date; a failed one needs saying so, or the counts above read as accepted.
function dispatchedAt(up) {
  if (!up.timestamp) return 'Never';
  const when = new Date(up.timestamp * 1000);
  const sameDay = when.toDateString() === new Date().toDateString();
  const stamp = sameDay ? when.toLocaleTimeString() : when.toLocaleString();
  return up.success === false ? `${stamp} (failed)` : stamp;
}

export async function refreshUserStats() {
  let data;
  try {
    data = await api.getUserStats();
  } catch {
    return;
  }
  setText('wdg-username', data.username || 'WDGWars User');
  setText('wdg-total-captures', fmtNum(data.total || 0));
  setText('wdg-wifi', fmtNum(data.wifi || 0));
  setText('wdg-ble', fmtNum(data.ble || 0));
  setText('wdg-adsb', fmtNum(data.aircraft ?? data.adsb ?? 0));
  setText('wdg-mesh', fmtNum(data.mesh || 0));
  setText('wdg-flock', fmtNum(data.flock ?? data.flock_count ?? 0));
}

export async function refreshTables() {
  try {
    table.setData(await api.getTables());
  } catch {
    /* transient; the status badge already reports connectivity */
  }
}

export async function refreshSnapshots() {
  let files;
  try {
    files = await api.getSnapshots();
  } catch {
    return;
  }

  setText('snapshot-count-label', `${files.length} Files`);
  const list = $('snapshot-history');
  if (files.length === 0) {
    list.innerHTML =
      '<div class="empty-state empty-state-compact">No payload snapshots archived yet.</div>';
    return;
  }
  // Name and counts are grouped separately so the row can stack them on a
  // narrow screen instead of letting a long filename crush the counts.
  list.innerHTML = files.map((f) => `
    <div class="history-item">
      <span class="hist-name" title="${esc(f.name)}">
        <svg class="icon" aria-hidden="true"><use href="#i-skull"/></svg>${esc(f.name)}</span>
      <span class="hist-stats">
        <span class="hist-ac">${esc(fmtNum(f.aircraft_count || 0))} aircraft</span>
        <span class="hist-mesh">${esc(fmtNum(f.mesh_nodes_count || 0))} mesh</span>
        <span class="hist-size">${((f.size || 0) / 1024).toFixed(1)} KB</span>
      </span>
    </div>`).join('');
}
