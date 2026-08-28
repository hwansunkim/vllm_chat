// frontend/js/sim/settings/textareas.js
// 설정 페이지의 긴 텍스트 입력 두 가지 장치:
//   1) auto-grow — 내용에 맞춰 높이가 늘어나되 420px 에서 멈춘다.
//   2) 확대 오버레이 에디터(⤢) — 정말 긴 글을 넓은 화면에서 고친다.
//
// auto-grow 는 `data-autogrow` 가 붙은 textarea 만 대상으로 한다.

const MAX_HEIGHT = 420;

/**
 * 한 개의 textarea 높이를 내용에 맞춘다.
 * MIN 은 CSS의 min-height 를 그대로 계승하고(필드마다 다름), MAX 는 공통 420px.
 *
 * display:none 인 조상(접힌 에이전트 카드, .sim-hidden 인 sys/inf 설정) 아래에서는
 * scrollHeight 가 0 이라 전부 MIN 으로 찌부러진다 — 그럴 땐 건드리지 않고,
 * 펼쳐지는 시점에 다시 부른다. (섹션 아코디언은 display:none 이 아니라
 * grid-template-rows:0fr 클리핑이라 접혀 있어도 정상적으로 계산된다.)
 */
export function autoGrow(el) {
  if (!el || !el.isConnected) return;
  if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return;

  const cs = getComputedStyle(el);
  // box-sizing: border-box 인 필드는 테두리 두께까지 height 에 포함되어야 한다.
  const extra = cs.boxSizing === 'border-box'
    ? (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0)
    : 0;
  const min = parseFloat(cs.minHeight) || 0;

  el.style.height = 'auto';
  const natural = el.scrollHeight + extra;
  const next = Math.min(MAX_HEIGHT, Math.max(min, natural));
  el.style.height = `${next}px`;
  // 상한에 닿았을 때만 내부 스크롤로 전환한다.
  el.style.overflowY = natural > MAX_HEIGHT ? 'auto' : 'hidden';
}

/** root(기본: 문서 전체) 아래의 모든 auto-grow 대상 높이를 다시 계산한다. */
export function autoGrowAll(root) {
  const scope = root || document;
  scope.querySelectorAll('textarea[data-autogrow]').forEach(autoGrow);
}

/**
 * 입력 시 즉시 늘어나도록 위임 리스너를 건다.
 * 위임이라 renderSettingsPage()가 새로 그려낸 에이전트 카드에도 자동으로 적용된다.
 */
export function initAutoGrow(root) {
  const scope = root || document.getElementById('sim-settings-view');
  if (!scope) return;
  scope.addEventListener('input', e => {
    const ta = e.target;
    if (ta instanceof HTMLTextAreaElement && ta.hasAttribute('data-autogrow')) autoGrow(ta);
  });

  // 기능 on-off 체크박스는 본문을 display:none 에서 되살리므로 그 직후 재계산이 필요하다.
  // page.js 는 chk.onchange = ... 로 프로퍼티를 덮어쓰므로 addEventListener 는 살아남는다.
  ['sim-sys-enabled', 'sim-inf-enabled'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => {
      requestAnimationFrame(() => autoGrowAll(document.getElementById('sim-settings-main')));
    });
  });
}

// ── 확대 오버레이 에디터 ──────────────────────────────────────────────────────

let _target = null;   // 편집 중인 원본 textarea

function _els() {
  return {
    overlay: document.getElementById('sim-text-editor-overlay'),
    area:    document.getElementById('sim-text-editor-area'),
    title:   document.getElementById('sim-text-editor-title'),
  };
}

function _openEditor(ta, titleText) {
  const { overlay, area, title } = _els();
  if (!overlay || !area) return;
  _target = ta;
  title.textContent = titleText || '텍스트 편집';
  area.value = ta.value;
  overlay.classList.remove('sim-hidden');
  area.focus();
  area.setSelectionRange(area.value.length, area.value.length);
}

function _closeEditor() {
  const { overlay } = _els();
  overlay?.classList.add('sim-hidden');
  _target = null;
}

function _saveEditor() {
  const { area } = _els();
  if (!_target || !area) return _closeEditor();
  const ta = _target;
  ta.value = area.value;
  // ★ 값만 대입하면 상태에 반영되지 않는다. 에이전트 카드의 system_prompt 는
  //   agents.js 의 [data-field] input 위임 리스너로만 sim 에 기록되기 때문에,
  //   여기서 input 을 재발사하지 않으면 저장 후 조용히 유실된다.
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  _closeEditor();
  autoGrow(ta);
  ta.focus();
}

/** ⤢ 버튼(위임) + 오버레이 단축키(Esc 닫기 / Ctrl·Cmd+Enter 저장). */
export function initTextEditorOverlay() {
  const { overlay, area } = _els();
  if (!overlay || !area) return;

  // ⤢ 버튼은 .sim-ta-wrap 안에 textarea 와 나란히 있다(정적 필드 + 동적 에이전트 카드 공통).
  document.addEventListener('click', e => {
    const btn = e.target.closest('.sim-zoom-btn');
    if (!btn) return;
    const wrap = btn.closest('.sim-ta-wrap');
    const ta   = wrap?.querySelector('textarea');
    if (!ta) return;
    e.preventDefault();
    _openEditor(ta, wrap.dataset.zoomTitle);
  });

  document.getElementById('sim-text-editor-save')?.addEventListener('click', _saveEditor);
  document.getElementById('sim-text-editor-cancel')?.addEventListener('click', _closeEditor);
  // 배경(오버레이 자체) 클릭 = 취소. 박스 내부 클릭은 무시한다.
  overlay.addEventListener('mousedown', e => { if (e.target === overlay) _closeEditor(); });

  area.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); _closeEditor(); }
    else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); _saveEditor(); }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !overlay.classList.contains('sim-hidden')) _closeEditor();
  });
}
