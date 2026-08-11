// The aircraft / mesh-node / dispatch-report table: tab state, sorting,
// filtering, rendering.

import { esc, feedResult, fmtCoord, fmtNum, fmtOpt, fmtRssi } from './format.js';

export const TABS = ['aircraft', 'mesh', 'reports'];

const COLUMNS = {
  aircraft: [
    ['icao', 'ICAO'], ['callsign', 'Callsign'], ['alt_ft', 'Altitude (ft)'],
    ['speed_kt', 'Speed (kt)'], ['heading', 'Heading'], ['first_seen', 'First Seen'],
  ],
  mesh: [
    ['node_id', 'Node ID'], ['node_type', 'Type'], ['lat', 'Latitude'],
    ['lon', 'Longitude'], ['rssi', 'RSSI (dBm)'], ['first_seen', 'First Seen'],
  ],
  // The two feed columns sort on what was sent; the counts they print are the
  // verdict WDGWars returned for it.
  reports: [
    ['timestamp', 'Dispatched'], ['window_hours', 'Window'], ['polls', 'Polls'],
    ['aircraft_count', 'Aircraft'], ['mesh_count', 'Mesh'],
  ],
};

const TITLES = {
  aircraft: ['Accumulated Airborne Telemetry',
             'Aircraft signals intercepted in current dead drop window'],
  mesh: ['Accumulated LoRa Mesh Telemetry',
         'Mesh nodes ingested from MeshMapper wardriving pings'],
  reports: ['Dispatch Reports',
            'What WDGWars made of each upload window, newest first'],
};

const DEFAULT_SORT = { aircraft: 'first_seen', mesh: 'first_seen', reports: 'timestamp' };

const EMPTY = {
  aircraft: 'No aircraft signals in window.',
  mesh: 'No mesh nodes ingested in current window.',
  reports: 'No dispatches yet — the first upload window has not closed.',
};

// What the search box matches per tab, and what identifies a row for the
// re-render check below.
const SEARCH_TEXT = {
  aircraft: (r) => `${r.icao || ''} ${r.callsign || ''}`,
  mesh: (r) => `${r.node_id || ''} ${r.node_type || ''}`,
  reports: (r) => [
    dispatchedAt(r),
    Object.keys(r.aircraft_reject_reasons || {}).join(' '),
    Object.keys(r.mesh_reject_reasons || {}).join(' '),
    r.success === false ? 'failed' : '',
    r.dry_run ? 'dry run' : '',
  ].join(' '),
};

const ROW_KEY = {
  aircraft: (r) => `${r.icao}${r.first_seen}`,
  mesh: (r) => `${r.node_id}${r.first_seen}`,
  reports: (r) => `${r.timestamp}${r.success}`,
};

const state = {
  tab: 'aircraft',
  aircraft: [],
  mesh: [],
  reports: [],
  sortCol: 'first_seen',
  sortAsc: false,
  lastRenderKey: '',
};

const $ = (id) => document.getElementById(id);

// Populates the mobile sort control for the tab the page opens on; switchTab
// refreshes it from then on.
export function init() {
  buildSortOptions();
}

export function setData({ aircraft, mesh }) {
  state.aircraft = aircraft;
  state.mesh = mesh;
  render();
}

// window_hours is derived here rather than server-side: the entry stores the
// window's ends, and this is the only place that wants it as a duration.
export function setReports(entries) {
  state.reports = entries.map((e) => ({
    ...e,
    window_hours: e.window_start && e.window_end
      ? Math.max(0, (e.window_end - e.window_start) / 3600) : 0,
  }));
  render();
}

export function currentTab() {
  return state.tab;
}

export function switchTab(tab) {
  state.tab = tab;
  for (const name of TABS) {
    const btn = $(`tab-${name}`);
    btn.classList.toggle('active', tab === name);
    btn.setAttribute('aria-selected', String(tab === name));
  }
  const [title, subtitle] = TITLES[tab];
  $('table-title').innerText = title;
  $('table-subtitle').innerText = subtitle;
  // Only the reports tab has an ingest summary to show above the table.
  $('ingest-summary').classList.toggle('visible', tab === 'reports');

  // The tabs share no columns, so a sort carried over from another one would
  // name a field that does not exist here.
  if (!COLUMNS[tab].some(([col]) => col === state.sortCol)) {
    state.sortCol = DEFAULT_SORT[tab];
    state.sortAsc = false;
  }
  buildSortOptions();
  render();
}

export function sortBy(col) {
  if (state.sortCol === col) state.sortAsc = !state.sortAsc;
  else { state.sortCol = col; state.sortAsc = true; }
  render();
}

// The column headers are hidden on narrow screens, where each row renders as a
// stacked card, so sorting needs a control that does not live in the <thead>.
export function setSortColumn(col) {
  if (state.sortCol === col) return;
  state.sortCol = col;
  state.sortAsc = true;
  render();
}

export function toggleSortDir() {
  state.sortAsc = !state.sortAsc;
  render();
}

function buildSortOptions() {
  const select = $('sort-select');
  if (!select) return;
  select.innerHTML = COLUMNS[state.tab]
    .map(([col, label]) => `<option value="${col}">${label}</option>`).join('');
  select.value = state.sortCol;
}

function syncSortControl() {
  const select = $('sort-select');
  if (select && select.value !== state.sortCol) select.value = state.sortCol;

  const dir = $('btn-sort-dir');
  if (!dir) return;
  dir.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-sort-` +
                  `${state.sortAsc ? 'asc' : 'desc'}"/></svg>`;
  dir.setAttribute('aria-label',
    state.sortAsc ? 'Sorted ascending — tap to sort descending'
                  : 'Sorted descending — tap to sort ascending');
}

function rowsFor(tab, query) {
  const filtered = state[tab].filter((r) =>
    SEARCH_TEXT[tab](r).toUpperCase().includes(query));

  return filtered.sort((a, b) => {
    let x = a[state.sortCol] ?? '';
    let y = b[state.sortCol] ?? '';
    if (typeof x === 'string') x = x.toUpperCase();
    if (typeof y === 'string') y = y.toUpperCase();
    return state.sortAsc ? (x < y ? -1 : 1) : (x > y ? -1 : 1);
  });
}

function aircraftCells(a) {
  return [
    `<span class="icao-badge">${esc(a.icao)}</span>`,
    a.callsign ? esc(a.callsign) : '<span class="text-dim">--</span>',
    fmtOpt(a.alt_ft),
    fmtOpt(a.speed_kt),
    fmtOpt(a.heading, '°'),
    esc(a.first_seen) || '--',
  ];
}

function meshCells(m) {
  return [
    `<span class="mesh-badge">${esc(m.node_id)}</span>`,
    `<span class="mesh-type">${esc(m.node_type || 'REPEATER')}</span>`,
    fmtCoord(m.lat),
    fmtCoord(m.lon),
    fmtRssi(m.rssi),
    esc(m.first_seen) || '--',
  ];
}

// A dispatch from a previous day needs its date; today's only needs the time.
function dispatchedAt(r) {
  if (!r.timestamp) return '--';
  const when = new Date(r.timestamp * 1000);
  return when.toDateString() === new Date().toDateString()
    ? when.toLocaleTimeString() : when.toLocaleString();
}

// A refusal and a failed delivery are different outcomes and read differently:
// refused means WDGWars saw the records and said no, not delivered means it
// never gave a verdict and the window was retained.
function feedCell(count, imported, success, rejected, reasons) {
  const cls = success === false ? 'feed-failed' : (rejected ? 'feed-refused' : 'feed-ok');
  return `<span class="${cls}">` +
         `${esc(feedResult(count, imported, success, rejected, reasons))}</span>`;
}

function reportCells(r) {
  const tag = r.dry_run ? '<span class="report-tag">dry run</span>' : '';
  return [
    `<span class="report-time">${esc(dispatchedAt(r))}</span>${tag}`,
    `${r.window_hours.toFixed(2)}h`,
    fmtNum(r.polls || 0),
    feedCell(r.aircraft_count, r.aircraft_imported, r.aircraft_success,
             r.aircraft_rejected, r.aircraft_reject_reasons),
    feedCell(r.mesh_count, r.mesh_imported, r.mesh_success,
             r.mesh_rejected, r.mesh_reject_reasons),
  ];
}

const CELLS = { aircraft: aircraftCells, mesh: meshCells, reports: reportCells };

// data-label carries the column name into each cell so the stacked mobile
// layout can print it alongside the value once the header row is hidden.
function rowHtml(cells) {
  const labels = COLUMNS[state.tab];
  return `<tr>${cells.map((html, i) =>
    `<td data-label="${labels[i][1]}">${html}</td>`).join('')}</tr>`;
}

export function render(force = false) {
  const query = $('search-input').value.toUpperCase();
  const source = state[state.tab];

  // Rebuilding identical rows on every poll throws away scroll position and any
  // in-progress text selection, so skip the write when nothing changed.
  const key = [state.tab, query, state.sortCol, state.sortAsc, source.length,
               source.map(ROW_KEY[state.tab]).join()].join('|');
  if (!force && key === state.lastRenderKey) return;
  state.lastRenderKey = key;

  const wrapper = document.querySelector('.table-wrapper');
  const scrollTop = wrapper ? wrapper.scrollTop : 0;

  // Drawn rather than typed: the arrow characters this used to emit render at
  // the mercy of the platform font, and U+2195 in particular defaults to emoji
  // presentation on iOS. The sorted column shows its actual direction.
  $('table-head').innerHTML =
    `<tr>${COLUMNS[state.tab].map(([col, label]) => {
      const active = col === state.sortCol;
      const sorted = active ? (state.sortAsc ? 'ascending' : 'descending') : 'none';
      const caret = active ? (state.sortAsc ? 'i-sort-asc' : 'i-sort-desc') : 'i-sort';
      return `<th data-sort="${col}" scope="col" aria-sort="${sorted}"` +
             `${active ? ' class="sorted"' : ''}>` +
             `${label} <svg class="icon sort-caret" aria-hidden="true">` +
             `<use href="#${caret}"/></svg></th>`;
    }).join('')}</tr>`;

  const rows = rowsFor(state.tab, query);
  if (rows.length === 0) {
    const empty = source.length === 0 ? EMPTY[state.tab] : 'No matching signals.';
    $('table-body').innerHTML = `<tr><td colspan="${COLUMNS[state.tab].length}" ` +
                                `class="empty-state">${empty}</td></tr>`;
  } else {
    const cellFn = CELLS[state.tab];
    $('table-body').innerHTML = rows.map((r) => rowHtml(cellFn(r))).join('');
  }

  syncSortControl();
  if (wrapper) wrapper.scrollTop = scrollTop;
}
