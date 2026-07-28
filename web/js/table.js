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

export function setData({ aircraft, mesh }) {
  state.aircraft = aircraft;
  state.mesh = mesh;
  render();
}

export function switchTab(tab) {
  state.tab = tab;
  $('tab-aircraft').classList.toggle('active', tab === 'aircraft');
  $('tab-mesh').classList.toggle('active', tab === 'mesh');
  const [title, subtitle] = TITLES[tab];
  $('table-title').innerText = title;
  $('table-subtitle').innerText = subtitle;
  render();
}

export function sortBy(col) {
  if (state.sortCol === col) state.sortAsc = !state.sortAsc;
  else { state.sortCol = col; state.sortAsc = true; }
  render();
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

function aircraftRow(a) {
  return `<tr>
    <td><span class="icao-badge">${esc(a.icao)}</span></td>
    <td>${a.callsign ? esc(a.callsign) : '<span class="text-dim">--</span>'}</td>
    <td>${fmtOpt(a.alt_ft)}</td>
    <td>${fmtOpt(a.speed_kt)}</td>
    <td>${fmtOpt(a.heading, '°')}</td>
    <td>${esc(a.first_seen) || '--'}</td>
  </tr>`;
}

function meshRow(m) {
  return `<tr>
    <td><span class="mesh-badge">${esc(m.node_id)}</span></td>
    <td class="mesh-type">${esc(m.node_type || 'REPEATER')}</td>
    <td>${fmtCoord(m.lat)}</td>
    <td>${fmtCoord(m.lon)}</td>
    <td>${fmtRssi(m.rssi)}</td>
    <td>${esc(m.first_seen) || '--'}</td>
  </tr>`;
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

  $('table-head').innerHTML =
    `<tr>${COLUMNS[state.tab].map(([col, label]) =>
      `<th data-sort="${col}">${label} ↕</th>`).join('')}</tr>`;

  const rows = rowsFor(state.tab, query);
  if (rows.length === 0) {
    const empty = source.length === 0
      ? (state.tab === 'aircraft' ? 'No aircraft signals in window.'
                                  : 'No mesh nodes ingested in current window.')
      : 'No matching signals.';
    $('table-body').innerHTML = `<tr><td colspan="6" class="empty-state">${empty}</td></tr>`;
  } else {
    const rowFn = state.tab === 'aircraft' ? aircraftRow : meshRow;
    $('table-body').innerHTML = rows.map(rowFn).join('');
  }

  if (wrapper) wrapper.scrollTop = scrollTop;
}
