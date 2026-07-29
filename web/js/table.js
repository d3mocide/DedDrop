// The aircraft / mesh-node table: tab state, sorting, filtering, rendering.

import { esc, fmtCoord, fmtOpt, fmtRssi } from './format.js';

const COLUMNS = {
  aircraft: [
    ['icao', 'ICAO'], ['callsign', 'Callsign'], ['alt_ft', 'Altitude (ft)'],
    ['speed_kt', 'Speed (kt)'], ['heading', 'Heading'], ['first_seen', 'First Seen'],
  ],
  mesh: [
    ['node_id', 'Node ID'], ['node_type', 'Type'], ['lat', 'Latitude'],
    ['lon', 'Longitude'], ['rssi', 'RSSI (dBm)'], ['first_seen', 'First Seen'],
  ],
};

const TITLES = {
  aircraft: ['Accumulated Airborne Telemetry',
             'Aircraft signals intercepted in current dead drop window'],
  mesh: ['Accumulated LoRa Mesh Telemetry',
         'Mesh nodes ingested from MeshMapper wardriving pings'],
};

const state = {
  tab: 'aircraft',
  aircraft: [],
  mesh: [],
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

export function switchTab(tab) {
  state.tab = tab;
  $('tab-aircraft').classList.toggle('active', tab === 'aircraft');
  $('tab-mesh').classList.toggle('active', tab === 'mesh');
  $('tab-aircraft').setAttribute('aria-selected', String(tab === 'aircraft'));
  $('tab-mesh').setAttribute('aria-selected', String(tab === 'mesh'));
  const [title, subtitle] = TITLES[tab];
  $('table-title').innerText = title;
  $('table-subtitle').innerText = subtitle;

  // The two tabs share no columns, so a sort carried over from the other one
  // would name a field that does not exist here.
  if (!COLUMNS[tab].some(([col]) => col === state.sortCol)) {
    state.sortCol = 'first_seen';
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
  dir.innerText = state.sortAsc ? '↑' : '↓';
  dir.setAttribute('aria-label',
    state.sortAsc ? 'Sorted ascending — tap to sort descending'
                  : 'Sorted descending — tap to sort ascending');
}

function rowsFor(tab, query) {
  const source = tab === 'aircraft' ? state.aircraft : state.mesh;
  const fields = tab === 'aircraft' ? ['icao', 'callsign'] : ['node_id', 'node_type'];
  const filtered = source.filter((r) =>
    fields.some((f) => (r[f] || '').toUpperCase().includes(query)));

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

// data-label carries the column name into each cell so the stacked mobile
// layout can print it alongside the value once the header row is hidden.
function rowHtml(cells) {
  const labels = COLUMNS[state.tab];
  return `<tr>${cells.map((html, i) =>
    `<td data-label="${labels[i][1]}">${html}</td>`).join('')}</tr>`;
}

export function render(force = false) {
  const query = $('search-input').value.toUpperCase();
  const source = state.tab === 'aircraft' ? state.aircraft : state.mesh;
  const idField = state.tab === 'aircraft' ? 'icao' : 'node_id';

  // Rebuilding identical rows on every poll throws away scroll position and any
  // in-progress text selection, so skip the write when nothing changed.
  const key = [state.tab, query, state.sortCol, state.sortAsc, source.length,
               source.map((r) => r[idField] + r.first_seen).join()].join('|');
  if (!force && key === state.lastRenderKey) return;
  state.lastRenderKey = key;

  const wrapper = document.querySelector('.table-wrapper');
  const scrollTop = wrapper ? wrapper.scrollTop : 0;

  // U+2195 defaults to emoji presentation on iOS and renders as a coloured
  // glyph out of step with the text, so the neutral arrow needs U+FE0E. The
  // sorted column shows its actual direction rather than the same idle hint.
  $('table-head').innerHTML =
    `<tr>${COLUMNS[state.tab].map(([col, label]) => {
      const active = col === state.sortCol;
      const sorted = active ? (state.sortAsc ? 'ascending' : 'descending') : 'none';
      const caret = active ? (state.sortAsc ? '↑' : '↓') : '↕︎';
      return `<th data-sort="${col}" scope="col" aria-sort="${sorted}"` +
             `${active ? ' class="sorted"' : ''}>` +
             `${label} <span class="sort-caret">${caret}</span></th>`;
    }).join('')}</tr>`;

  const rows = rowsFor(state.tab, query);
  if (rows.length === 0) {
    const empty = source.length === 0
      ? (state.tab === 'aircraft' ? 'No aircraft signals in window.'
                                  : 'No mesh nodes ingested in current window.')
      : 'No matching signals.';
    $('table-body').innerHTML = `<tr><td colspan="6" class="empty-state">${empty}</td></tr>`;
  } else {
    const cellFn = state.tab === 'aircraft' ? aircraftCells : meshCells;
    $('table-body').innerHTML = rows.map((r) => rowHtml(cellFn(r))).join('');
  }

  syncSortControl();
  if (wrapper) wrapper.scrollTop = scrollTop;
}
