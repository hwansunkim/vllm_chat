// frontend/js/sim/run/errors.js
// 실행 중 발생한 오류를 누적하고, 상태 배지 옆의 "⚠ N건" 버튼 + 클릭 토글 팝업으로 보여준다.
//
// 오류는 두 종류다.
//  1) turn_error — 백엔드(ABM/simulation/step.py)가 턴별 LLM 호출 실패 시 보내는 SSE 이벤트.
//     payload에 turn / speaker / error(메시지 문자열)가 들어있다.
//  2) EventSource 'error' — 연결 자체가 끊긴 경우. 브라우저 스펙상 메시지 데이터가 없어서
//     고정 안내 문구만 남길 수 있다.
//
// 시나리오가 막히는 증상은 보통 turn_error가 여러 턴에 걸쳐 반복되며 롤백을 거듭하는
// 형태이므로, 마지막 하나가 아니라 최근 MAX_ERROR_LOG건을 턴/화자와 함께 누적해 보여준다.

import { sim, esc, agentLabel, MAX_ERROR_LOG } from '../state.js';

const CONNECTION_ERROR_MSG = '연결이 끊겼습니다. (서버 스트림 종료 또는 네트워크 오류)';

function badgeEl() { return document.getElementById('sim-error-badge'); }
function popupEl() { return document.getElementById('sim-error-popup'); }
function wrapEl()  { return document.getElementById('sim-error-wrap'); }

function nowStr() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function pushError(entry) {
  sim.errorLog.push({ ...entry, timestamp: nowStr() });
  // 상한 초과분은 앞(오래된 쪽)에서 잘라낸다.
  if (sim.errorLog.length > MAX_ERROR_LOG) {
    sim.errorLog.splice(0, sim.errorLog.length - MAX_ERROR_LOG);
  }
  renderErrorIndicator();
}

/** turn_error SSE payload를 오류 로그에 기록. */
export function recordTurnError(d) {
  const p = d || {};
  pushError({
    kind:    'turn',
    turn:    (p.turn === 0 || p.turn) ? p.turn : null,
    speaker: p.speaker || '',
    error:   String(p.error || '알 수 없는 오류'),
  });
}

/** EventSource 연결 오류를 같은 로그에 기록 (고정 문구). */
export function recordConnectionError() {
  pushError({ kind: 'connection', turn: null, speaker: '', error: CONNECTION_ERROR_MSG });
}

/** 새 실행 시작 시 이전 실행의 오류를 비운다. */
export function clearErrorLog() {
  sim.errorLog = [];
  closeErrorPopup();
  renderErrorIndicator();
}

// ── 렌더링 ───────────────────────────────────────────────────────────────────

/** 배지 표시/개수 갱신. 팝업이 열려 있으면 내용도 함께 다시 그린다. */
export function renderErrorIndicator() {
  const badge = badgeEl();
  const popup = popupEl();
  if (!badge || !popup) return;

  const n = sim.errorLog.length;
  badge.classList.toggle('sim-hidden', n === 0);
  badge.textContent = `⚠ ${n}건`;
  badge.title = `실행 오류 ${n}건 — 클릭해 상세 보기`;

  if (n === 0) { closeErrorPopup(); return; }
  if (!popup.classList.contains('sim-hidden')) renderErrorPopup();
}

function renderErrorPopup() {
  const popup = popupEl();
  if (!popup) return;

  // 최신순 — 원본 배열은 오래된 것부터이므로 복사본을 뒤집는다.
  const items = sim.errorLog.slice().reverse().map(e => {
    const meta = e.kind === 'connection'
      ? '<span class="sim-error-where conn">연결</span>'
      : `<span class="sim-error-where">${e.turn === null ? '턴 -' : `턴 ${esc(e.turn)}`}</span>` +
        `<span class="sim-error-speaker">${esc(e.speaker ? agentLabel(e.speaker) : '알 수 없음')}</span>`;
    return `<li class="sim-error-item">
      <div class="sim-error-meta">${meta}<span class="sim-error-time">${esc(e.timestamp)}</span></div>
      <div class="sim-error-msg">${esc(e.error)}</div>
    </li>`;
  }).join('');

  // 오류 메시지는 LLM/서버에서 온 임의 문자열이므로 위에서 전부 esc() 처리했다.
  popup.innerHTML = `
    <div class="sim-error-popup-head">
      <span>실행 오류 ${sim.errorLog.length}건 (최신순)</span>
      <button type="button" id="sim-error-popup-close" class="sim-error-popup-close" title="닫기">✕</button>
    </div>
    <ul class="sim-error-list">${items}</ul>
    <div class="sim-error-popup-foot">최근 ${MAX_ERROR_LOG}건까지 보관되며, 새로 시작하면 초기화됩니다.</div>`;
}

// ── 열기/닫기 ────────────────────────────────────────────────────────────────

export function openErrorPopup() {
  const popup = popupEl();
  const badge = badgeEl();
  if (!popup || !badge || !sim.errorLog.length) return;
  renderErrorPopup();
  popup.classList.remove('sim-hidden');
  badge.classList.add('open');
  badge.setAttribute('aria-expanded', 'true');
}

export function closeErrorPopup() {
  const popup = popupEl();
  const badge = badgeEl();
  if (popup) popup.classList.add('sim-hidden');
  if (badge) {
    badge.classList.remove('open');
    badge.setAttribute('aria-expanded', 'false');
  }
}

export function toggleErrorPopup() {
  const popup = popupEl();
  if (!popup) return;
  if (popup.classList.contains('sim-hidden')) openErrorPopup();
  else closeErrorPopup();
}

// ── 이벤트 바인딩 (앱 초기화 시 1회) ─────────────────────────────────────────

export function initErrorPopupEvents() {
  badgeEl()?.addEventListener('click', toggleErrorPopup);

  // 팝업 내용은 재렌더되므로 컨테이너에 위임 바인딩한다.
  popupEl()?.addEventListener('click', e => {
    if (e.target.closest('#sim-error-popup-close')) closeErrorPopup();
  });

  // 바깥 클릭 / ESC 로 닫기 (배지 자신의 클릭은 wrap 안이라 토글만 동작한다)
  document.addEventListener('click', e => {
    const wrap = wrapEl();
    if (wrap && !wrap.contains(e.target)) closeErrorPopup();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeErrorPopup();
  });

  renderErrorIndicator();
}
