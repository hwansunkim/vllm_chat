// frontend/js/sim/utils/download.js
// Generic browser download helpers (Blob + object URL) + filename utilities.

/**
 * Trigger a browser download of `content` as a file named `filename`.
 *
 * @param {string} content - File contents.
 * @param {string} filename - Suggested download filename.
 * @param {string} mimeType - MIME type for the Blob (e.g. 'text/markdown;charset=utf-8').
 */
export function downloadFile(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Sanitize a string for safe use as a filename (strip path-unsafe chars, cap length).
 *
 * @param {string} str
 * @returns {string}
 */
export function safeFilename(str) {
  return str.replace(/[/\\:*?"<>|]/g, '_').slice(0, 80);
}

/**
 * Compact, filename-safe timestamp tag for the current moment (e.g. "2026-08-04_1234").
 *
 * @returns {string}
 */
export function nowTag() {
  return new Date().toISOString().slice(0, 16).replace('T', '_').replace(':', '');
}
