// frontend/js/sim/settings/contract-preview.js
// 엔진 계약 미리보기(읽기 전용) + 출력 계약 오버라이드(opt-in).
//
// ── 왜 미리보기인가 ──────────────────────────────────────────────────────────
// 출력 형식·이동 규칙 같은 "엔진 계약"은 더 이상 시나리오에 얼려 저장되지 않는다.
// 엔진이 실행 시점에 현재 config 로 매번 생성한다(ABM/prompt_contract.py).
// 사용자는 그것을 편집하지 않고 **확인**만 하면 되므로, 여기서는 서버가 돌려준
// 문자열을 그대로 <pre> 에 보여준다. 응답의 `contract` 는 실제 주입본과 글자
// 단위로 같다(백엔드 테스트가 이 성질을 고정한다).
//
// ── 왜 "누구 기준" 인가 ──────────────────────────────────────────────────────
// 관계 지도(AgentConfig.relationships)가 생기면서 계약은 더 이상 전원 공유가 아니다.
// 엔진은 공유 world 계약 뒤에 **화자 시점의** `[아는 사람 (나와의 관계)]` 블록을 이어
// 붙이고 `<TARGETS>` 에도 관계 라벨을 단다(core._apply_engine_contract). 그래서 이
// 패널은 기준 에이전트를 하나 고르고 그 사람의 relationships 를 요청에 실어야 실제
// 주입본과 같아진다. 아무도 관계를 안 쓰면 예전처럼 전원이 같은 계약을 받는다.
//
// ── 갱신 트리거 ──────────────────────────────────────────────────────────────
// 계약 문자열을 바꾸는 입력은 6가지다:
//   location_graph(노드/is_exterior/zone), time_mode+time_per_wave,
//   infection_model.enabled+disease_name, extra_fields, output_format_override,
//   그리고 기준 에이전트의 relationships(+ 이름/표시이름).
// 설정 페이지 전체에 위임 리스너를 걸되, 디바운스 후 위 항목으로 만든 **서명**이
// 실제로 달라졌을 때만 요청을 보낸다. 그래서 무관한 입력(예: max_waves 타이핑)은
// 네트워크를 전혀 건드리지 않는다.

import { sim, getAgentIcon, liveRelationships, buildKeyToAlias } from '../state.js';
import { readLocationGraph } from './location-graph.js';
import { updateSectionBadges } from './sections.js';
import { autoGrowAll } from './textareas.js';

const DEBOUNCE_MS = 400;

let _timer      = null;
let _lastSig    = null;   // 마지막으로 요청을 보낸 시점의 서명
let _dirty      = true;   // 접혀 있는 동안 설정이 바뀌었는가
let _inflight   = 0;      // 응답 순서 뒤집힘 방지용 요청 일련번호
// 오버라이드 체크를 껐다 켜는 사이 사용자가 쓴 글을 잃지 않기 위한 임시 보관.
// 꺼져 있는 동안 sim.output_format_override 는 '' 이어야 하므로(= 오버라이드 없음)
// 상태가 아니라 여기에 둔다.
let _stashedOverride = '';
// 계약을 "누구 기준"으로 그릴지. name 이 아니라 **객체 참조**로 들고 있는다 — 이름은
// 사용자가 언제든 고치는 값이라, name 으로 붙잡으면 기준 에이전트의 ID 를 한 글자
// 고치는 순간 기준이 조용히 첫 사람으로 튄다. 참조는 rename 을 그냥 통과하고,
// 시나리오를 갈아끼우면(= 새 객체 배열) 자연히 무효가 된다.
let _previewAgentRef = null;

// ── DOM 헬퍼 ─────────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

function _els() {
  return {
    details:  $('sim-contract-details'),
    agentSel: $('sim-contract-agent'),
    flags:    $('sim-contract-flags'),
    status:   $('sim-contract-status'),
    warnings: $('sim-contract-warnings'),
    pre:      $('sim-contract-pre'),
    chk:      $('sim-output-override-enabled'),
    box:      $('sim-output-override-box'),
    ta:       $('sim-output-override'),
  };
}

// ── 요청 페이로드 ────────────────────────────────────────────────────────────

/** 오버라이드가 켜져 있을 때만 그 문자열을 돌려준다(꺼져 있으면 항상 ''). */
function _currentOverride() {
  const { chk, ta } = _els();
  if (!chk || !ta) return sim.output_format_override || '';
  return chk.checked ? ta.value : '';
}

// ── 기준 에이전트 ────────────────────────────────────────────────────────────

/** 지금 계약을 그릴 기준 에이전트. 선택이 없거나 명부에서 사라졌으면 첫 사람. */
function _previewAgent() {
  if (!sim.agents?.length) return null;
  if (_previewAgentRef && sim.agents.includes(_previewAgentRef)) return _previewAgentRef;
  return sim.agents[0];
}

/**
 * 기준 에이전트의 관계 조각. dangling(없는 상대·자기 자신)은 **프론트에서** 걷어낸다.
 * 서버는 명부를 모르니 검증하지 않고(schemas.py 주석), 엔진은 실행 시점에
 * `_sanitize_relationships` 로 같은 규칙으로 버린다. 여기서 미리 맞춰야 미리보기가
 * "실제 주입본과 글자 단위로 같다"는 이 패널의 약속을 지킨다. 걸러진 관계는 사라진 게
 * 아니라 에이전트 카드에서 dangling 배지로 계속 보인다.
 */
function _relationshipPayload() {
  const agent = _previewAgent();
  const rels  = liveRelationships(agent);
  if (!Object.keys(rels).length) {
    // 관계 미사용 = 관계 도입 전과 100% 같은 요청(자리표시자 타깃 하나).
    return { relationships: {}, key_to_alias: {}, available_targets: [] };
  }
  return {
    relationships:     rels,
    // 관계 블록과 <TARGETS> 라벨이 같은 맵을 본다 — 안 보내면 이름이 key 로만 렌더된다.
    key_to_alias:      buildKeyToAlias(),
    // 아는 사람들을 더미 타깃으로 세워, 위치 그래프가 없는 시나리오에서 <TARGETS> 의
    // 관계 라벨(` · ` 구분)이 실제로 어떻게 붙는지 보이게 한다.
    available_targets: Object.keys(rels),
  };
}

/**
 * 계약 프리뷰 요청 본문. SimStartConfig 의 부분집합이라 편집 중인 값을 그대로 보낸다.
 * 관계를 쓰지 않으면 available_targets / key_to_alias 는 비워 보낸다 — 서버가
 * 자리표시자 하나로 렌더하고, 그 옆의 안내 문구가 "실행 중에는 같은 자리의 사람들로
 * 채워진다"를 말한다.
 */
function _buildPayload(overrideOverride) {
  const timeModeEl = $('sim-time-mode');
  const timePerEl  = $('sim-time-per-wave');
  const infEnabled = $('sim-inf-enabled');
  const infName    = $('sim-inf-disease-name');
  const graph      = readLocationGraph();
  return {
    location_graph: graph,
    // 위치 그래프가 있으면 실행 중 <TARGETS>는 flat 목록이 아니라 "[현재 상황]에서
    // 확인" 안내가 된다(step.py의 sit_targets). 프리뷰도 같은 블록을 그리도록.
    situation_targets: graph.length > 0,
    time_mode:      timeModeEl?.value === 'variable' ? 'variable' : 'fixed',
    time_per_wave:  parseInt(timePerEl?.value ?? sim.time_per_wave ?? 30) || 0,
    infection_model: {
      enabled:      infEnabled ? infEnabled.checked : !!sim.infection_model?.enabled,
      disease_name: (infName ? infName.value : (sim.infection_model?.disease_name || '')).trim(),
    },
    // 이름이 빈 필드("+ 추가" 직후의 빈 줄)는 계약에 실리지 않는다 — 보내면 미리보기에만
    // 유령 줄이 생기고, 타이핑 한 글자마다 서명이 흔들려 요청도 늘어난다.
    extra_fields:   (sim.extra_fields || [])
      .filter(f => (f.name || '').trim())
      .map(f => ({ name: f.name, default: f.default || '' })),
    output_format_override: overrideOverride ?? _currentOverride(),
    include_output_schema:  true,
    // 관계는 per-agent — "누구 기준" 셀렉트가 고른 한 명의 시점만 보낸다.
    ..._relationshipPayload(),
  };
}

/** 계약을 바꾸는 입력만 담은 서명. 이게 같으면 재요청하지 않는다. */
function _signature() {
  const p = _buildPayload();
  return JSON.stringify([
    p.location_graph.map(n => [n.name, n.is_exterior, n.zone, n.is_zone_entry]),
    p.time_mode, p.time_per_wave,
    p.infection_model.enabled, p.infection_model.disease_name,
    p.extra_fields,
    p.output_format_override,
    p.relationships, p.key_to_alias, p.available_targets,
  ]);
}

// ── 렌더링 ───────────────────────────────────────────────────────────────────

const FLAG_LABELS = [
  ['has_location_graph', '🗺 지도'],
  ['has_zone',           '🏘 zone'],
  ['time_enabled',       '🕐 시간'],
  ['infection_enabled',  '🦠 감염'],
];

function _renderFlags(flags) {
  const { flags: el } = _els();
  if (!el) return;
  el.textContent = '';
  if (!flags) return;
  FLAG_LABELS.forEach(([key, label]) => {
    const span = document.createElement('span');
    span.className = `sim-contract-flag${flags[key] ? ' on' : ''}`;
    span.textContent = label;
    span.title = flags[key] ? `${label} 계약이 주입됩니다` : `${label} 계약은 주입되지 않습니다`;
    el.appendChild(span);
  });

  // 관계는 서버 flags 에 없다(ContractFlags 는 계약 검증 어서션과 1:1 이고 관계는
  // 검증 대상이 아니다). 기준 에이전트가 관계를 쓸 때만 클라이언트 쪽 칩을 덧붙인다.
  const relCount = Object.keys(liveRelationships(_previewAgent())).length;
  if (!relCount) return;
  const chip = document.createElement('span');
  chip.className   = 'sim-contract-flag on';
  chip.textContent = `🔗 관계 ${relCount}`;
  chip.title = `[아는 사람 (나와의 관계)] 블록에 ${relCount}명이 실립니다 (이 에이전트 시점).`;
  el.appendChild(chip);
}

function _renderWarnings(warnings) {
  const { warnings: el } = _els();
  if (!el) return;
  el.textContent = '';
  const list = warnings || [];
  el.classList.toggle('sim-hidden', !list.length);
  if (!list.length) return;
  const head = document.createElement('div');
  head.className = 'sim-contract-warning-head';
  head.textContent = `⚠ 계약 진단 ${list.length}건`;
  el.appendChild(head);
  const ul = document.createElement('ul');
  list.forEach(w => {
    const li = document.createElement('li');
    li.textContent = w;          // textContent — 서버 문자열을 마크업으로 해석하지 않는다
    ul.appendChild(li);
  });
  el.appendChild(ul);
}

function _setStatus(text, isError = false) {
  const { status } = _els();
  if (!status) return;
  status.textContent = text || '';
  status.classList.toggle('sim-hidden', !text);
  status.classList.toggle('error', !!isError);
}

// ── 서버 호출 ────────────────────────────────────────────────────────────────

async function _postPreview(payload) {
  const res = await fetch('/api/simulation/contract-preview', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function _refresh() {
  const { pre } = _els();
  if (!pre) return;
  const sig = _signature();
  const seq = ++_inflight;
  _lastSig = sig;
  _dirty   = false;
  _setStatus('계약을 생성하는 중…');
  try {
    const data = await _postPreview(_buildPayload());
    if (seq !== _inflight) return;             // 더 최신 요청이 이미 떠 있다
    pre.textContent = data.contract || '';     // 실제 주입본과 글자 단위로 동일
    _renderFlags(data.flags);
    _renderWarnings(data.warnings);
    _setStatus('');
  } catch (e) {
    if (seq !== _inflight) return;
    console.error('[sim] 계약 미리보기 실패:', e);
    _lastSig = null;                           // 다음 기회에 다시 시도한다
    pre.textContent = '';
    _renderFlags(null);
    _renderWarnings(null);
    _setStatus(`미리보기를 불러오지 못했습니다 (${e.message}). ↻ 새로고침으로 다시 시도하세요.`, true);
  }
}

/**
 * 접혀 있을 때는 요청하지 않는다 — 대신 dirty 로 표시해두고 펼치는 순간 받아온다.
 * force=true 면 서명이 같아도 다시 받아온다(↻ 새로고침 버튼).
 */
function _maybeRefresh(force = false) {
  const { details } = _els();
  if (!details) return;
  const sig = _signature();
  if (!force && sig === _lastSig && !_dirty) return;
  if (!details.open) { _dirty = true; return; }
  _refresh();
}

/** 설정 입력 → 디바운스 → 서명 비교 → (필요할 때만) 요청. */
export function scheduleContractPreview() {
  clearTimeout(_timer);
  _timer = setTimeout(() => _maybeRefresh(false), DEBOUNCE_MS);
}

// ── 오버라이드 (D3) ──────────────────────────────────────────────────────────

function _paintOverride(enabled) {
  const { box } = _els();
  box?.classList.toggle('sim-hidden', !enabled);
}

/** 엔진이 생성한 출력 계약을 오버라이드 편집기에 채운다(오버라이드 없이 한 번 더 요청). */
async function _loadGeneratedIntoOverride() {
  const { ta } = _els();
  if (!ta) return;
  try {
    const data = await _postPreview(_buildPayload(''));   // override 비운 상태의 생성본
    ta.value = data.output_contract || '';
    sim.output_format_override = ta.value;
    ta.dispatchEvent(new Event('input', { bubbles: true }));  // auto-grow 재계산
  } catch (e) {
    console.error('[sim] 생성된 출력 계약 불러오기 실패:', e);
    _setStatus(`생성된 계약을 불러오지 못했습니다 (${e.message}).`, true);
  }
}

// ── 공개 API ─────────────────────────────────────────────────────────────────

/**
 * "누구 기준" 셀렉트를 현재 에이전트 명부로 다시 채운다.
 * 에이전트 추가/삭제/이름·표시이름 변경 때 agents.js 가 호출한다.
 */
export function refreshContractAgentSelect() {
  const { agentSel } = _els();
  if (!agentSel) return;

  const agents = sim.agents || [];
  agentSel.textContent = '';
  agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = `${getAgentIcon(a, 'neutral')} ${a.display_name || a.name}`;
    agentSel.appendChild(opt);
  });

  const active = _previewAgent();
  if (!agents.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(에이전트 없음)';
    agentSel.appendChild(opt);
  }
  agentSel.disabled = agents.length < 2;   // 한 명뿐이면 고를 게 없다
  agentSel.value    = active ? active.name : '';
  // 삭제/시나리오 교체로 선택이 폴백됐으면 참조도 따라 옮긴다.
  _previewAgentRef  = active;
}

/** renderSettingsPage() 에서 호출 — 상태 → 폼. */
export function renderContractPreview() {
  const { chk, ta } = _els();
  refreshContractAgentSelect();
  const ov = sim.output_format_override || '';
  if (chk) chk.checked = !!ov;
  // 현재 시나리오 기준으로만 채운다 — 다른 시나리오에서 남은 _stashedOverride 를
  // 끌어오면 안 된다(체크 시 그대로 승격돼 A 의 오버라이드가 B 로 샌다).
  if (ta)  ta.value    = ov;
  _stashedOverride     = ov;
  _paintOverride(!!ov);
  // 시나리오를 갈아끼웠을 수 있으므로 캐시를 버리고 다시 판정한다.
  _lastSig = null;
  _dirty   = true;
  _maybeRefresh(false);
}

/** readConfigFromUI() 에서 호출 — 폼 → 상태. 체크가 꺼져 있으면 언제나 ''. */
export function readOutputFormatOverride() {
  return _currentOverride();
}

export function initContractPreview() {
  const { details, chk, ta, agentSel } = _els();
  if (!details) return;

  details.addEventListener('toggle', () => {
    if (details.open) _maybeRefresh(false);
  });

  // 기준 에이전트 변경은 사용자가 결과를 바로 기다리는 조작이라 디바운스를 건너뛴다.
  agentSel?.addEventListener('change', () => {
    _previewAgentRef = sim.agents?.find(a => a.name === agentSel.value) || null;
    if (!details.open) details.open = true;   // 접혀 있으면 왜 아무 일도 없는지 알 수 없다
    _maybeRefresh(false);
  });

  $('sim-contract-refresh-btn')?.addEventListener('click', () => {
    if (!details.open) details.open = true;
    _maybeRefresh(true);
  });

  $('sim-contract-load-generated-btn')?.addEventListener('click', _loadGeneratedIntoOverride);

  chk?.addEventListener('change', () => {
    if (chk.checked) {
      if (ta && !ta.value.trim() && _stashedOverride) ta.value = _stashedOverride;
      sim.output_format_override = ta ? ta.value : '';
      _paintOverride(true);
      if (ta && !ta.value.trim()) _loadGeneratedIntoOverride();
      autoGrowAll(document.getElementById('sim-output-override-box'));
    } else {
      _stashedOverride = ta ? ta.value : '';
      sim.output_format_override = '';
      _paintOverride(false);
    }
    updateSectionBadges(sim);
    scheduleContractPreview();
  });

  ta?.addEventListener('input', () => {
    if (chk?.checked) sim.output_format_override = ta.value;
    updateSectionBadges(sim);
  });

  // 설정 페이지 전체 위임 — 어떤 입력이든 일단 예약하고, 서명이 같으면 조용히 접는다.
  // (위치 그래프의 노드 삭제·연결 추가는 click 으로만 끝나는 경로가 있어 click 도 본다.
  //  location-graph.js 의 container.onclick 이 먼저 실행되므로 여기서는 이미 갱신된
  //  sim.location_graph 를 읽는다 — 버블링 순서상 자식이 먼저다.)
  const main = document.getElementById('sim-settings-main');
  ['input', 'change', 'click'].forEach(evt => {
    main?.addEventListener(evt, scheduleContractPreview);
  });
}
