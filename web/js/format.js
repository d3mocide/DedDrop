// Value formatting and HTML escaping.

// Callsigns come off the ADS-B feed and node ids from whoever can POST
// /api/wardrive, so nothing reaching innerHTML is trusted.
export function esc(v) {
  if (v === undefined || v === null) return '';
  return String(v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// A plain space here lets "16 275" break across two lines in a narrow table
// cell or stat card, which reads as two separate numbers. U+00A0 groups the
// digits identically but never wraps.
export function fmtNum(n) {
  if (n === undefined || n === null) return '0';
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
}

// "28 sent / 0 new" on its own reads as a mystery. A refusal WDGWars itemised
// is the answer, so it goes on the same line. Shared by the dispatch panel and
// the report table so both describe a dispatch the same way.
export function feedResult(count, imported, success, rejected, reasons) {
  let line = `${fmtNum(count || 0)} sent / ${fmtNum(imported || 0)} new`;
  if (rejected) {
    const why = Object.keys(reasons || {}).join(', ');
    line += ` / ${fmtNum(rejected)} refused${why ? ` (${why})` : ''}`;
  }
  return success === false ? `${line} — not delivered` : line;
}

const UNKNOWN = '<span class="text-dim">--</span>';

// Missing telemetry is null on the wire, not a fabricated 0, so render it as
// unknown rather than implying stationary or heading due north.
export function fmtOpt(v, suffix = '') {
  if (v === undefined || v === null) return UNKNOWN;
  return esc(fmtNum(v)) + suffix;
}

export function fmtCoord(v) {
  return typeof v === 'number' ? v.toFixed(5) : '--';
}

export function fmtRssi(v) {
  return v === null || v === undefined ? UNKNOWN : `${esc(v)} dBm`;
}
