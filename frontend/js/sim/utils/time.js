// frontend/js/sim/utils/time.js
// Unified time formatting helper. Replaces 3 near-duplicates from the old monolith.

export const statusIcon = {
  done:    '✅',
  stopped: '⏹',
  running: '🔄',
  error:   '❌',
  default: '⏳',
};

/**
 * Format a UNIX timestamp (seconds) into MM-DD HH:MM or YYYY-MM-DD HH:MM.
 *
 * @param {number} ts                          - UNIX timestamp in seconds.
 * @param {{ includeYear?: boolean }} [opts]   - Pass `{ includeYear: true }` for full date.
 * @returns {string} formatted time or `'—'` when `ts` is falsy.
 */
export function fmtTime(ts, opts = {}) {
  if (!ts) return '—';
  const d   = new Date(ts * 1000);
  const mm  = String(d.getMonth() + 1).padStart(2, '0');
  const dd  = String(d.getDate()).padStart(2, '0');
  const hh  = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  if (opts.includeYear) {
    return `${d.getFullYear()}-${mm}-${dd} ${hh}:${min}`;
  }
  return `${mm}-${dd} ${hh}:${min}`;
}
