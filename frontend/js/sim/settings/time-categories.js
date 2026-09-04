// frontend/js/sim/settings/time-categories.js
// 시간 모드(고정/가변) 토글과 가변 시간 카테고리 · idle 스케줄 에디터.

import { sim, esc, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE } from '../state.js';
import { updateTargetDurationUI } from './target-duration.js';
import { updateSectionBadges } from './sections.js';

// ── 가변 시간 모드 UI 연동 ────────────────────────────────────────────────────

export function updateVariableTimeUI(mode) {
  const section = document.getElementById('sim-variable-time-section');
  if (section) section.classList.toggle('sim-hidden', mode !== 'variable');
}

// ── 시간 추론 방식 (time_estimation_mode) ─────────────────────────────────────
// time_mode='variable'일 때만 의미 있는 하위 옵션이라 가변 시간 섹션 안에 산다.
// 'category'(기본) = LLM이 카테고리를 고르고 그 범위에서 randint,
// 'ai'            = LLM이 경과분을 직접 추론(값은 카테고리 전체 min~max로 clamp).
// AI 모드에서도 카테고리 min/max 에디터는 숨기지 않는다 — 그 범위가 sanity clamp로
// 재사용되기 때문이다(안내 문구만 바꿔 끼운다).

export function normalizeTimeEstimationMode(v) {
  return v === 'ai' ? 'ai' : 'category';
}

export function updateTimeEstimationModeUI(mode) {
  const ai = normalizeTimeEstimationMode(mode) === 'ai';
  document.getElementById('sim-time-cat-hint')?.classList.toggle('sim-hidden', ai);
  document.getElementById('sim-time-ai-hint')?.classList.toggle('sim-hidden', !ai);
}

// 셀렉트가 DOM에 없는 경로(설정 패널 렌더 전 저장 등)에서는 기존 상태 → 기본값으로 폴백한다.
export function readTimeEstimationMode() {
  const sel = document.getElementById('sim-time-estimation-mode');
  return normalizeTimeEstimationMode(sel ? sel.value : sim.time_estimation_mode);
}

export function initTimeEstimationModeToggle() {
  const sel = document.getElementById('sim-time-estimation-mode');
  if (!sel) return;
  sel.onchange = () => {
    sim.time_estimation_mode = normalizeTimeEstimationMode(sel.value);
    updateTimeEstimationModeUI(sim.time_estimation_mode);
    updateSectionBadges(sim);   // 접힌 섹션 헤더/네비 뱃지("가변 · AI 추론")를 즉시 갱신
  };
}

// 카테고리 개수는 자유다 — 백엔드(TimeCategory/_non_empty_categories)는 "비어 있지만 않으면"
// 개수 제한이 없다. 행은 전부 sim.time_categories 배열을 기준으로 동적으로 렌더링한다.
export function renderTimeCategories() {
  const container = document.getElementById('sim-time-cat-list');
  if (!container) return;

  // 상태가 비어 있으면 기본 카테고리로 시드한다 — 이후 추가/삭제가 실제 배열을 조작해야 하므로
  // DEFAULT_TIME_CATEGORIES를 그대로 참조하지 않고 복사본을 상태에 심는다.
  if (!sim.time_categories?.length) {
    sim.time_categories = DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
  }
  const cats = sim.time_categories;
  container.innerHTML = '';

  cats.forEach((cat, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-time-cat-row';
    row.dataset.idx   = String(idx);
    row.dataset.catId = cat.id;
    // 마지막 1개는 삭제할 수 없다 — 백엔드가 빈 카테고리 목록을 422로 거부한다.
    const canDelete = cats.length > 1;
    row.innerHTML = `
      <input type="text" class="sim-time-cat-label" value="${esc(cat.label)}" placeholder="카테고리 ${idx + 1}"/>
      <input type="number" class="sim-time-cat-min" value="${esc(cat.min_minutes)}" min="1" max="1440"/>
      <span class="sim-time-cat-sep">~</span>
      <input type="number" class="sim-time-cat-max" value="${esc(cat.max_minutes)}" min="1" max="1440"/>
      <span class="sim-time-cat-unit">분</span>
      <button class="sim-time-cat-del" data-idx="${idx}" title="${canDelete ? '카테고리 삭제' : '카테고리는 최소 1개 필요합니다'}"
              ${canDelete ? '' : 'disabled'}>×</button>`;
    container.appendChild(row);
  });

  // 이벤트 위임 — renderSettingsPage()가 여러 번 호출되므로 addEventListener 대신
  // onclick으로 덮어써서 리스너가 중복 등록되지 않게 한다.
  container.onclick = e => {
    const del = e.target.closest('.sim-time-cat-del');
    if (!del || del.disabled) return;
    // 삭제 전 화면의 편집 중인 값을 상태에 먼저 반영한다 — 아래 재렌더링이 DOM을 통째로
    // 다시 그리므로, 저장하지 않으면 사용자가 방금 고친 label/min/max가 사라진다.
    sim.time_categories = readTimeCategories();
    const i = parseInt(del.dataset.idx);
    if (sim.time_categories.length <= 1) return;
    sim.time_categories.splice(i, 1);
    renderTimeCategories();
  };
}

// 렌더된 행 수만큼 읽는다. 값이 비었거나 범위를 벗어나면 안전한 기본값으로 보정한다
// (백엔드는 min_minutes >= 1, max_minutes >= min_minutes를 요구함).
export function readTimeCategories() {
  const rows = document.querySelectorAll('#sim-time-cat-list .sim-time-cat-row');
  const result = [];
  rows.forEach((row, idx) => {
    const id = row.dataset.catId || _newTimeCatId(result.map(c => c.id));
    const labelEl = row.querySelector('.sim-time-cat-label');
    const minEl   = row.querySelector('.sim-time-cat-min');
    const maxEl   = row.querySelector('.sim-time-cat-max');
    const label   = labelEl?.value.trim() || `카테고리 ${idx + 1}`;
    let min = parseInt(minEl?.value);
    if (isNaN(min) || min < 1) min = 5;
    let max = parseInt(maxEl?.value);
    if (isNaN(max) || max < min) max = min;
    result.push({ id, label, min_minutes: min, max_minutes: max });
  });
  // 행이 하나도 없으면(가변 시간 UI가 렌더되기 전 저장 등) 기존 상태 → 기본값 순으로 폴백한다.
  if (result.length) return result;
  if (sim.time_categories?.length) return sim.time_categories.map(c => ({ ...c }));
  return DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
}

// 기존 id와 겹치지 않는 새 id를 만든다. 백엔드는 id에 형식 제약이 없지만(순수 str),
// LLM 분류 프롬프트에 그대로 나열되므로 예측 가능한 형태를 유지한다.
function _newTimeCatId(existingIds) {
  const taken = new Set(existingIds || (sim.time_categories || []).map(c => c.id));
  let n = taken.size + 1;
  while (taken.has(`custom_${n}`)) n++;
  return `custom_${n}`;
}

// 가변 시간 "+ 카테고리 추가" 버튼
export function addTimeCategory() {
  // 편집 중인 값을 먼저 상태로 흡수한 뒤 새 항목을 덧붙인다 (삭제 핸들러와 동일한 이유).
  sim.time_categories = readTimeCategories();
  sim.time_categories.push({
    id: _newTimeCatId(sim.time_categories.map(c => c.id)),
    label: `카테고리 ${sim.time_categories.length + 1}`,
    min_minutes: 15,
    max_minutes: 30,
  });
  renderTimeCategories();
}

// idle_minutes_schedule: 콤마 구분 텍스트 → 정수 배열. 빈 배열이 되면 안 됨(백엔드 422 방지).
export function readIdleSchedule() {
  const raw = document.getElementById('sim-idle-schedule')?.value || '';
  const nums = raw.split(',')
    .map(s => parseInt(s.trim()))
    .filter(n => !isNaN(n) && n > 0);
  return nums.length ? nums : [...DEFAULT_IDLE_MINUTES_SCHEDULE];
}

export function initTimeModeToggle() {
  const sel = document.getElementById('sim-time-mode');
  if (!sel) return;
  sel.onchange = () => {
    sim.time_mode = sel.value === 'variable' ? 'variable' : 'fixed';
    updateVariableTimeUI(sim.time_mode);
    // 'fixed' + wave당 시간 0 조합에서만 목표 기간이 비활성이므로 모드 전환 시 함께 갱신한다.
    updateTargetDurationUI();
    updateSectionBadges(sim);   // 접힌 섹션 헤더/네비 뱃지("가변"/"N분/wave")를 즉시 갱신
  };
}
