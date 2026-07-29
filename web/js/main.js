// Entry point: wires DOM events and starts the refresh loops.
//
// All handlers are attached here rather than as inline onclick= attributes,
// which a strict Content-Security-Policy blocks.

import * as api from './api.js';
import * as panels from './panels.js';
import * as table from './table.js';
import * as ui from './ui.js';

const $ = (id) => document.getElementById(id);

// How fast each thing actually changes: telemetry every few seconds, the
// archive once per upload window, remote profile stats server-cached for 60s.
const STATUS_INTERVAL_MS = 5000;
const SNAPSHOT_INTERVAL_MS = 30000;
const USER_STATS_INTERVAL_MS = 60000;

function refreshLive() {
  panels.refreshStatus();
  panels.refreshTables();
}

function refreshAll() {
  refreshLive();
  panels.refreshSnapshots();
  panels.refreshUserStats();
}

// Polling a backgrounded page costs a phone battery and radio time for results
// nobody is looking at, so the timers only run while the page is visible.
const POLLS = [
  [refreshLive, STATUS_INTERVAL_MS],
  [panels.refreshSnapshots, SNAPSHOT_INTERVAL_MS],
  [panels.refreshUserStats, USER_STATS_INTERVAL_MS],
];

let timers = [];

function startPolling() {
  if (timers.length) return;
  timers = POLLS.map(([fn, interval]) => setInterval(fn, interval));
}

function stopPolling() {
  timers.forEach(clearInterval);
  timers = [];
}

function onVisibilityChange() {
  if (document.hidden) {
    stopPolling();
    return;
  }
  // What is on screen was fetched before the page was hidden, and the dispatch
  // timer has kept running the whole time. Refresh on the way back in rather
  // than showing however stale that is until the next tick.
  refreshAll();
  startPolling();
}

async function runTrigger(button, action, delayMs) {
  button.disabled = true;
  try {
    const { message } = await action();
    ui.showToast(message);
    setTimeout(refreshLive, delayMs);
  } catch {
    ui.showToast('Request failed — is DedDrop still running?');
  } finally {
    button.disabled = false;
  }
}

function wireEvents() {
  $('btn-meshmapper').addEventListener('click', ui.openMeshMapperModal);
  $('btn-poll').addEventListener('click', (e) =>
    runTrigger(e.currentTarget, api.triggerPoll, 1000));
  $('btn-flush').addEventListener('click', (e) =>
    runTrigger(e.currentTarget, api.triggerFlush, 1500));

  $('btn-copy-link').addEventListener('click', ui.copyMeshMapperLink);
  $('btn-close-modal').addEventListener('click', ui.closeMeshMapperModal);
  $('meshmapper-modal').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) ui.closeMeshMapperModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') ui.closeMeshMapperModal();
  });

  $('tab-aircraft').addEventListener('click', () => table.switchTab('aircraft'));
  $('tab-mesh').addEventListener('click', () => table.switchTab('mesh'));
  $('search-input').addEventListener('input', () => table.render());

  // Stands in for the column headers on narrow screens, where rows render as
  // stacked cards and the <thead> is hidden.
  $('sort-select').addEventListener('change', (e) =>
    table.setSortColumn(e.currentTarget.value));
  $('btn-sort-dir').addEventListener('click', table.toggleSortDir);

  // Delegated: the header row is re-rendered whenever the tab changes.
  $('table-head').addEventListener('click', (e) => {
    const col = e.target.closest('th')?.dataset.sort;
    if (col) table.sortBy(col);
  });
}

function start() {
  wireEvents();
  table.init();

  refreshAll();

  // A page opened in a background tab starts with no timers; the first
  // visibilitychange starts them.
  if (!document.hidden) startPolling();
  document.addEventListener('visibilitychange', onVisibilityChange);
}

// Modules are deferred by default, so the DOM is already parsed.
start();
