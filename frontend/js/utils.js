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
