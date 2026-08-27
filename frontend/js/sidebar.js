// 사이드바 접기/펼치기 — 상태는 localStorage에 영속화한다.
const STORAGE_KEY = 'vllm-chat.sidebar-collapsed';
const COLLAPSED_CLASS = 'sidebar-collapsed';

// 프라이빗 브라우징 등에서 localStorage 접근 자체가 예외를 던질 수 있으므로
// 읽기/쓰기 모두 try/catch로 감싸고, 실패하면 기본값(펼침)으로 동작한다.
function readCollapsed() {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1';
  } catch (e) {
    return false;
  }
}

function saveCollapsed(collapsed) {
  try {
    localStorage.setItem(STORAGE_KEY, collapsed ? '1' : '0');
  } catch (e) {
    /* 저장 불가 — 이번 세션에서만 상태를 유지한다 */
  }
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
