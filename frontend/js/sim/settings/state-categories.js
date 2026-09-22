// frontend/js/sim/settings/state-categories.js
// 에이전트 상태(수면·개인 용무) 카테고리 에디터 — time-categories.js와 같은 패턴.
//
// id는 화면에 노출되지 않는 순수 내부 키다(_resolve_state_category가 알 수 없는
// id를 첫 행으로 폴백하는 것과 같은 이유 — 사용자는 label·min·max만 편집한다).

import { sim, esc, DEFAULT_STATE_CATEGORIES } from '../state.js';

// 카테고리 개수는 자유다(백엔드가 "비어있지만 않으면" 개수 제한 없음) — 단 시간
// 카테고리와 달리 **빈 목록 자체가 유효한 값**이다(자기-선언형 상태 기능 off).
// 그래서 마지막 1개를 강제로 남기지 않는다 — time-categories.js와 다른 점.
export function renderStateCategories() {
  const container = document.getElementById('sim-state-cat-list');
  if (!container) return;

  const cats = sim.state_categories || [];
  container.innerHTML = '';

  cats.forEach((cat, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-time-cat-row';
    row.dataset.idx   = String(idx);
    row.dataset.catId = cat.id;
    row.innerHTML = `
      <input type="text" class="sim-time-cat-label" value="${esc(cat.label)}" placeholder="상태 ${idx + 1}"/>
      <input type="number" class="sim-time-cat-min" value="${esc(cat.min_minutes)}" min="1" max="1440"/>
      <span class="sim-time-cat-sep">~</span>
      <input type="number" class="sim-time-cat-max" value="${esc(cat.max_minutes)}" min="1" max="1440"/>
      <span class="sim-time-cat-unit">분</span>
      <button class="sim-time-cat-del" data-idx="${idx}" title="상태 삭제">×</button>`;
    container.appendChild(row);
  });

  container.onclick = e => {
    const del = e.target.closest('.sim-time-cat-del');
    if (!del) return;
    // 삭제 전 편집 중인 값을 먼저 상태에 반영한다 — 아래 재렌더링이 DOM을
    // 통째로 다시 그리므로, 저장하지 않으면 방금 고친 값이 사라진다.
    sim.state_categories = readStateCategories();
    const i = parseInt(del.dataset.idx);
    sim.state_categories.splice(i, 1);
    renderStateCategories();
  };
}

// 렌더된 행 수만큼 읽는다. 비었거나 범위를 벗어나면 안전한 기본값으로 보정한다
// (백엔드는 min_minutes >= 1, max_minutes >= min_minutes를 요구함).
export function readStateCategories() {
  const rows = document.querySelectorAll('#sim-state-cat-list .sim-time-cat-row');
  const result = [];
  rows.forEach((row, idx) => {
    const id = row.dataset.catId || _newStateCatId(result.map(c => c.id));
    const labelEl = row.querySelector('.sim-time-cat-label');
    const minEl   = row.querySelector('.sim-time-cat-min');
    const maxEl   = row.querySelector('.sim-time-cat-max');
    const label   = labelEl?.value.trim() || `상태 ${idx + 1}`;
    let min = parseInt(minEl?.value);
    if (isNaN(min) || min < 1) min = 5;
    let max = parseInt(maxEl?.value);
    if (isNaN(max) || max < min) max = min;
    result.push({ id, label, min_minutes: min, max_minutes: max });
  });
  // 행이 하나도 없으면(패널이 렌더되기 전 저장 등) 기존 상태를 그대로 보존한다 —
  // "행이 0개"와 "사용자가 의도적으로 다 지움"을 이 경로에서는 구분할 수 없어,
  // time-categories.js와 달리 빈 배열로 덮어쓰지 않는다(자기-선언형 상태를 쓰던
  // 시나리오가 패널을 열지 않은 채 저장했다가 기능을 잃는 것을 막는다).
  if (result.length || document.getElementById('sim-state-cat-list')) return result;
  return sim.state_categories?.length ? sim.state_categories.map(c => ({ ...c })) : [];
}

function _newStateCatId(existingIds) {
  const taken = new Set(existingIds || (sim.state_categories || []).map(c => c.id));
  let n = taken.size + 1;
  while (taken.has(`custom_${n}`)) n++;
  return `custom_${n}`;
}

// 가변 상태 "+ 상태 추가" 버튼
export function addStateCategory() {
  sim.state_categories = readStateCategories();
  sim.state_categories.push({
    id: _newStateCatId(sim.state_categories.map(c => c.id)),
    label: `상태 ${sim.state_categories.length + 1}`,
    min_minutes: 10,
    max_minutes: 30,
  });
  renderStateCategories();
}

// "기본값으로 복원" — 처음 켜는 사용자를 위한 지름길(빈 목록에서 시작하면
// "카테고리를 하나도 안 만들면 무슨 일이 생기는지" 알기 어렵다).
export function resetStateCategoriesToDefault() {
  sim.state_categories = DEFAULT_STATE_CATEGORIES.map(c => ({ ...c }));
  renderStateCategories();
}
