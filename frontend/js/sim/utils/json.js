// frontend/js/sim/utils/json.js
// Hardened code-fence stripping for assistant JSON responses.

// Matches ```...``` or ```json ... ``` with optional surrounding whitespace.
const _FENCE_RE = /^\s*```(?:[a-zA-Z0-9_-]+)?\s*\r?\n?([\s\S]*?)\r?\n?\s*```\s*$/;

/**
 * Strip a single Markdown code fence (optionally tagged like ```json) from a string.
 * Returns the inner content; if no fence is found, returns the input unchanged.
 *
 * @param {string} text - The raw assistant message content.
 * @returns {string}
 */
export function stripCodeFence(text) {
  if (typeof text !== 'string') return text;
  const m = text.match(_FENCE_RE);
  return m ? m[1] : text;
}
