// frontend/js/sim/settings/sections.js
// 설정 페이지의 섹션 아코디언 (다중 개방) + 좌측 네비 레일 + scroll-spy.
//
// ── 영속화 정책 ───────────────────────────────────────────────────────────────
// 섹션 = 시나리오와 무관한 **안정 ID**(background/agents/run/…) → localStorage에 영속.
// 에이전트 카드 = 키가 agent.name 이라 **시나리오 종속** → state.js의 _expandedAgents
// (Set)로 세션 전용. 이 비대칭은 의도적이다. 시나리오를 바꾸면 카드 펼침은 초기화되어야
// 하지만, "감염병 섹션은 늘 접어둔다" 같은 사용자 습관은 유지되어야 한다.
//
// 접기 클리핑은 .sim-settings-section 이 아니라 .sim-sec-wrap 에 건다.
// (simulation.css 의 `.sim-settings-section:has(.sim-help:hover){overflow:visible}` 규칙이
//  ? 툴팁에 호버하는 순간 섹션의 overflow 를 풀어버리므로, 섹션에 클리핑을 걸면
//  툴팁을 스치기만 해도 접힌 내용이 튀어나온다.)

import { readJSON, writeJSON } from '../../utils.js';
import { sim } from '../state.js';
import { autoGrowAll } from './textareas.js';

const STORAGE_KEY = 'vllm-chat.sim-settings-sections';
const COLLAPSED_CLASS = 'sim-sec-collapsed';

// "에이전트 3명으로 최소 시나리오를 돌리는 데 반드시 채워야 하는가" → 펼침.
const DEFAULT_EXPANDED = {
  background: true, agents: true, run: true,
  time: false, tuning: false, output: false,
  world: false, director: false, infection: false, events: false,
};

// 모든 시나리오에 기본으로 깔리는 출력 필드 — 이것만 있으면 "설정한 게 없다"로 본다.
const BASELINE_OUTPUT_FIELDS = new Set(['emotion', 'action', 'action_note']);

// 사용자가 명시적으로 접거나 편 섹션만 여기에 담긴다(부분 저장).
// 값이 없는 섹션은 콘텐츠 기반 자동 펼침 → 기본값 순으로 판정한다.
let _userState = _readUserState();

function _readUserState() {
  const raw = readJSON(STORAGE_KEY, {});
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const out = {};
  for (const [k, v] of Object.entries(raw)) {
    if (k in DEFAULT_EXPANDED) out[k] = !!v;
  }
  return out;
}

function _sections() {
  return Array.from(document.querySelectorAll('#sim-settings-main .sim-settings-section[data-section]'));
}

function _sectionEl(id) {
  return document.querySelector(`#sim-settings-main .sim-settings-section[data-section="${id}"]`);
}

/**
 * 콘텐츠 기반 자동 펼침 — "저장된 시나리오를 불러왔는데 설정한 게 사라진 것처럼"
 * 보이지 않게 한다. 사용자가 직접 접은 섹션에는 적용하지 않는다(우선순위 규칙).
 * 판정할 수 없으면 null 을 돌려 기본값으로 넘긴다.
 */
function _autoExpand(id, sim) {
  if (!sim) return null;
  switch (id) {
    case 'world':     return (sim.location_graph?.length ?? 0) > 0;
    case 'infection': return !!sim.infection_model?.enabled;
    case 'director':  return !!sim.system_agent?.enabled;
    case 'events':    return (sim.events?.length ?? 0) > 0;
    case 'time':      return sim.time_mode === 'variable';
    // extra_fields 는 emotion/action/action_note 3종이 항상 시드된다(state.js:232,
    // applyScenario 도 action_note 를 강제로 채운다). "비어 있지 않으면 펼침"으로 두면
    // 사실상 언제나 펼쳐져 접기의 의미가 사라지므로, 기본 3종을 넘어선 게 있을 때만 펼친다.
    case 'output':
      return !!sim.output_format_override ||
             (sim.extra_fields || []).some(f => !BASELINE_OUTPUT_FIELDS.has(f.name));
    default:          return null;
  }
}

function _resolveExpanded(id, sim) {
  if (id in _userState) return _userState[id];           // 1. 사용자 선택이 최우선
  const auto = _autoExpand(id, sim);
  if (auto !== null) return auto;                        // 2. 콘텐츠 기반 자동 펼침
  return DEFAULT_EXPANDED[id] ?? false;                  // 3. 기본값
}

function _paint(el, expanded) {
  el.classList.toggle(COLLAPSED_CLASS, !expanded);
  const btn = el.querySelector('.sim-sec-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    btn.title = expanded ? '섹션 접기' : '섹션 펼치기';
  }
  const navItem = document.querySelector(`#sim-settings-nav .sim-nav-item[data-nav="${el.dataset.section}"]`);
  navItem?.classList.toggle('collapsed', !expanded);
}

/** 저장된 상태 / 콘텐츠 / 기본값을 종합해 모든 섹션의 펼침 상태를 다시 칠한다. */
export function applySectionState(sim) {
  _sections().forEach(el => _paint(el, _resolveExpanded(el.dataset.section, sim)));
}

/** 사용자 토글 — 에이전트 카드의 _toggleExpand()와 같은 역할·같은 네이밍. */
function _toggleSection(id) {
  const el = _sectionEl(id);
  if (!el) return;
  const next = el.classList.contains(COLLAPSED_CLASS);
  _userState[id] = next;
  writeJSON(STORAGE_KEY, _userState);
  _paint(el, next);
  // 펼치는 순간 재계산하지 않으면, 클리핑 중 잡힌 높이 그대로 남아 어색해진다.
  if (next) autoGrowAll(el);
}

/** 접혀 있으면 펼친다(이미 펼쳐져 있으면 no-op). 토글이 아니므로 실수로 접히지 않는다. */
export function expandSection(id) {
  const el = _sectionEl(id);
  if (el && el.classList.contains(COLLAPSED_CLASS)) _toggleSection(id);
}

export function expandAll()   { _setAll(true); }
export function collapseAll() { _setAll(false); }

function _setAll(expanded) {
  _sections().forEach(el => {
    _userState[el.dataset.section] = expanded;
    _paint(el, expanded);
  });
  writeJSON(STORAGE_KEY, _userState);
  if (expanded) autoGrowAll(document.getElementById('sim-settings-main'));
}

// ── 요약 뱃지 ────────────────────────────────────────────────────────────────
// 접기의 최대 리스크는 "안 보이니 존재를 잊는다". 헤더(와 레일)에 상태를 남겨 해소한다.

function _badgeText(id, sim) {
  if (!sim) return '';
  switch (id) {
    case 'background': return sim.background?.trim() ? '' : '비어 있음';
    case 'agents':     return String(sim.agents?.length ?? 0);
    case 'run': {
      const parts = [`${sim.max_waves ?? 10} wave`];
      if (sim.target_duration_minutes) parts.push('목표 기간');
      return parts.join(' · ');
    }
    case 'time':
      if (sim.time_mode === 'variable') return '가변';
      return (sim.time_per_wave ?? 0) > 0 ? `${sim.time_per_wave}분/wave` : '시간 없음';
    case 'tuning':
      return (sim.summary_interval ?? 0) > 0 ? `요약 ${sim.summary_interval}w` : '';
    case 'output': {
      const n = sim.extra_fields?.length ?? 0;
      // "오버라이드"는 출력 계약을 직접 편집 중일 때만 — 엔진 생성분은 기본이라 배지가 없다.
      return sim.output_format_override ? `필드 ${n} · 오버라이드` : `필드 ${n}`;
    }
    case 'world':
      return sim.location_graph?.length ? `장소 ${sim.location_graph.length}` : '';
    case 'director':
      return sim.system_agent?.enabled ? `${sim.system_agent.display_name || '내레이터'} · ON` : '';
    case 'infection':
      return sim.infection_model?.enabled
        ? `${sim.infection_model.disease_name?.trim() || '이름 없음'} · ON`
        : '';
    case 'events':
      return sim.events?.length ? String(sim.events.length) : '';
    default: return '';
  }
}

/** renderSettingsPage() 끝에서 한 번에 갱신 — 헤더 뱃지와 레일 뱃지를 같은 값으로 맞춘다. */
export function updateSectionBadges(sim) {
  _sections().forEach(el => {
    const id   = el.dataset.section;
    const text = _badgeText(id, sim);
    const head = el.querySelector(`.sim-sec-badge[data-sec-badge="${id}"]`);
    if (head) {
      head.textContent = text;
      head.classList.toggle('sim-hidden', !text);
    }
    const nav = document.querySelector(`#sim-settings-nav .sim-nav-item[data-nav="${id}"] .sim-nav-badge`);
    if (nav) {
      nav.textContent = text;
      nav.classList.toggle('sim-hidden', !text);
    }
  });
}

// ── 좌측 네비 레일 ────────────────────────────────────────────────────────────

function _buildNav() {
  const nav = document.getElementById('sim-settings-nav');
  if (!nav) return;
  nav.textContent = '';

  const list = document.createElement('div');
  list.className = 'sim-nav-list';
  _sections().forEach(el => {
    const id = el.dataset.section;
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'sim-nav-item';
    item.dataset.nav = id;

    const icon = document.createElement('span');
    icon.className = 'sim-nav-icon';
    icon.textContent = el.dataset.secIcon || '•';

    const label = document.createElement('span');
    label.className = 'sim-nav-label';
    label.textContent = el.dataset.secLabel || id;

    const badge = document.createElement('span');
    badge.className = 'sim-nav-badge sim-hidden';

    item.append(icon, label, badge);
    item.addEventListener('click', () => _gotoSection(id));
    list.appendChild(item);
  });
  nav.appendChild(list);

  const actions = document.createElement('div');
  actions.className = 'sim-nav-actions';
  const expand = document.createElement('button');
  expand.type = 'button';
  expand.className = 'sim-nav-action';
  expand.textContent = '모두 펼치기';
  expand.addEventListener('click', expandAll);
  const collapse = document.createElement('button');
  collapse.type = 'button';
  collapse.className = 'sim-nav-action';
  collapse.textContent = '모두 접기';
  collapse.addEventListener('click', collapseAll);
  actions.append(expand, collapse);
  nav.appendChild(actions);
}

function _gotoSection(id) {
  const el = _sectionEl(id);
  if (!el) return;
  if (el.classList.contains(COLLAPSED_CLASS)) _toggleSection(id);
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  _setActiveNav(id);
}

function _setActiveNav(id) {
  document.querySelectorAll('#sim-settings-nav .sim-nav-item').forEach(it => {
    it.classList.toggle('active', it.dataset.nav === id);
  });
}

// scroll-spy — 화면에 보이는 섹션 중 문서 순서상 가장 앞선 것을 활성으로 본다.
function _initScrollSpy() {
  const root = document.getElementById('sim-settings-body');
  if (!root || typeof IntersectionObserver === 'undefined') return;

  const visible = new Set();
  const order   = _sections().map(el => el.dataset.section);
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      const id = e.target.dataset.section;
      if (e.isIntersecting) visible.add(id); else visible.delete(id);
    });
    const first = order.find(id => visible.has(id));
    if (first) _setActiveNav(first);
  }, { root, rootMargin: '0px 0px -65% 0px', threshold: 0 });

  _sections().forEach(el => io.observe(el));
}

// ── 초기화 ───────────────────────────────────────────────────────────────────

export function initSettingsSections() {
  const body = document.getElementById('sim-settings-body');
  if (!body) return;

  _buildNav();

  // 헤더 영역 클릭 → 토글. 버튼/입력/라벨(=활성화 체크박스)과 ? 툴팁은 제외한다.
  // 기능 on-off 체크박스는 섹션이 접힌 상태에서도 눌러야 하므로 헤더에 남겨둔다.
  document.querySelectorAll('#sim-settings-main .sim-settings-section-title').forEach(title => {
    title.addEventListener('click', e => {
      const el = title.closest('.sim-settings-section[data-section]');
      if (!el) return;
      const toggleBtn = e.target.closest('.sim-sec-toggle');
      if (!toggleBtn) {
        if (e.target.closest('button') || e.target.closest('input') ||
            e.target.closest('label')  || e.target.closest('.sim-help')) return;
      }
      _toggleSection(el.dataset.section);
    });
  });

  // 기능을 켜는 것은 "이제 이걸 설정하겠다"는 명시적 의사표시다. 섹션이 접혀 있으면
  // 체크만 하고 아무 일도 안 일어난 것처럼 보이므로 그 자리에서 펼쳐 준다.
  // (page.js가 chk.onchange 프로퍼티를 덮어쓰므로 addEventListener 로 붙여야 살아남는다.)
  [['sim-inf-enabled', 'infection'], ['sim-sys-enabled', 'director']].forEach(([chkId, secId]) => {
    document.getElementById(chkId)?.addEventListener('change', e => {
      if (e.target.checked) expandSection(secId);
      // page.js 의 chk.onchange 가 sim 에 반영하는 건 이 리스너 다음이다(등록 순서).
      // 뱃지는 한 프레임 뒤에 갱신해야 최신 값을 본다.
      requestAnimationFrame(() => updateSectionBadges(sim));
    });
  });

  // 저장된 상태를 먼저 칠하고 다음 프레임에 트랜지션을 켠다 (layout.css의 .sidebar-ready 선례).
  applySectionState(null);
  requestAnimationFrame(() => body.classList.add('sections-ready'));

  _initScrollSpy();
}
