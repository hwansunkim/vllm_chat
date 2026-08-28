export function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function scrollToBottom() {
  const el = document.getElementById('messages');
  el.scrollTop = el.scrollHeight;
}

export function removeEmptyState() {
  document.getElementById('empty-state')?.remove();
}

// ── localStorage 헬퍼 ────────────────────────────────────────────────────────
// 프라이빗 브라우징 등에서는 localStorage 접근 자체가 예외를 던질 수 있고,
// 저장된 값이 손상돼 JSON.parse가 깨질 수도 있다. 두 경우 모두 조용히 기본값으로
// 폴백해서 UI가 멈추지 않게 한다. (원래 js/sidebar.js에 있던 패턴을 승격한 것)

export function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    const val = JSON.parse(raw);
    return val === null || val === undefined ? fallback : val;
  } catch (e) {
    return fallback;
  }
}

export function writeJSON(key, val) {
  try {
    localStorage.setItem(key, JSON.stringify(val));
  } catch (e) {
    /* 저장 불가 — 이번 세션에서만 상태를 유지한다 */
  }
}
