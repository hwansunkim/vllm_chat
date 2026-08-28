// 사이드바 접기/펼치기 — 상태는 localStorage에 영속화한다.
import { readJSON, writeJSON } from './utils.js';

const STORAGE_KEY = 'vllm-chat.sidebar-collapsed';
const COLLAPSED_CLASS = 'sidebar-collapsed';

// 접근 불가(프라이빗 브라우징) / 손상된 값 처리는 utils의 readJSON/writeJSON이 맡는다.
// 구버전은 '1'/'0' 문자열로 저장했는데 JSON.parse('1') === 1 이라 그대로 호환된다.
function readCollapsed() {
  return !!readJSON(STORAGE_KEY, false);
}

function saveCollapsed(collapsed) {
  writeJSON(STORAGE_KEY, collapsed);
}

function applyCollapsed(collapsed) {
  document.body.classList.toggle(COLLAPSED_CLASS, collapsed);

  const btn = document.getElementById('sidebar-toggle');
  if (!btn) return;
  const label = collapsed ? '사이드바 펼치기' : '사이드바 접기';
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  btn.setAttribute('aria-label', label);
  btn.title = label;
}

export function initSidebarEvents() {
  // 저장된 상태를 먼저 적용한 뒤 다음 프레임에 트랜지션을 켠다.
  // (로드 직후 사이드바가 스르륵 접히는 잔상 방지)
  applyCollapsed(readCollapsed());
  requestAnimationFrame(() => document.body.classList.add('sidebar-ready'));

  const btn = document.getElementById('sidebar-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const next = !document.body.classList.contains(COLLAPSED_CLASS);
    applyCollapsed(next);
    saveCollapsed(next);
  });
}
