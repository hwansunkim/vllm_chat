// frontend/js/sim/settings/infection-config.js
// 감염병 모델(SEPIR) 설정 — 폼 ↔ sim.infection_model 동기화. 서버 검증(beta >= 0,
// 모든 분 값 0~52560000, max >= min, 분포 파라미터 > 0)을 통과하도록 읽어들이는 모든
// 경로가 buildInfectionModel()을 거친다.
//
// 단위 규칙: 전염(β)만 wave·접촉 기준이고, 증상 단계 진행은 "노출 후 경과 분"
// 기준이다. 잠복기(E)·무증상 전염기(P)·감염기(I) 지속 시간은 DurationSpec(분포
// 종류 + 파라미터 + min~max 절단 범위, **일 단위**)으로 편집한다 — 엔진 샘플러는
// ABM/simulation/infection.py::_sample_duration_minutes. 증상 단계는 사람이 쓰기
// 편하도록 … (증상 문구는 이제 시간창이 없다 — 상태(E/P/I) + 그 상태 안의 순서만 편집한다.)
// 모델 선택(sir/seir/sepir)은 UI 전용이다 — 쓰지 않는 구간은 편집기를 숨기고 지속을 0으로 강제한다.

import { sim, esc, buildInfectionModel, normalizeBeta, normalizeDurationSpec,
         DEFAULT_EXPOSED_DURATION, DEFAULT_PRESYMPTOMATIC_DURATION, DEFAULT_INFECTIOUS_DURATION,
         normalizeSymptomStages, SYMPTOM_STATUSES, SYMPTOM_STATUS_LABELS,
         MODEL_TYPES, visibleDurationKeys, applyModelType, isStatusAlwaysSkipped,
         isTimeConceptDisabled, getAgentIcon } from '../state.js';
import { renderScenarioEvents, INFECT_START_STATUS } from './events.js';

export function renderInfectionConfig() {
  const model = sim.infection_model = buildInfectionModel(sim.infection_model);
  const chk   = document.getElementById('sim-inf-enabled');
  const cfg   = document.getElementById('sim-inf-config');
  if (!chk || !cfg) return;

  chk.checked = model.enabled;
  cfg.classList.toggle('sim-hidden', !model.enabled);

  const nameEl = document.getElementById('sim-inf-disease-name');
  if (nameEl) nameEl.value = model.disease_name;
  const immuneEl = document.getElementById('sim-inf-immune');
  if (immuneEl) immuneEl.value = model.immune_after_recovery ? 'sir' : 'sis';

  _bindBetaInput();
  _bindModelType();
  _renderDurationEditors();
  renderPatientZeroPicker();
  _renderSymptomStages();
  updateInfectionTimeWarning();
  _bindTimeWatchers();

  // renderSettingsPage()는 여러 번 호출되므로 addEventListener 대신 onchange로 덮어쓴다
  // (renderSystemAgentConfig / renderServerSelect와 같은 규칙 — 리스너 중복 등록 방지).
  chk.onchange = () => {
    sim.infection_model.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
    // 켜는 순간 환자 0번이 하나도 없으면 아무 일도 일어나지 않는다 — 지금 알린다.
    updatePatientZeroWarning();
    // infect_agent 이벤트는 모델이 꺼져 있으면 서버에서 무시된다 —
    // 이벤트 에디터의 경고 배지를 지금 상태에 맞춰 다시 그린다.
    renderScenarioEvents();
  };
  if (nameEl)   nameEl.oninput    = () => { sim.infection_model.disease_name = nameEl.value.trim(); };
  if (immuneEl) immuneEl.onchange = () => { sim.infection_model.immune_after_recovery = immuneEl.value !== 'sis'; };
}

/** β(전염 계수) 입력 + 현재 값 표시. 값은 즉시 상태에 반영된다. */
function _bindBetaInput() {
  const input = document.getElementById('sim-inf-beta');
  const valEl = document.getElementById('sim-inf-beta-val');
  if (!input) return;
  const paint = () => { if (valEl) valEl.textContent = String(sim.infection_model.beta); };
  input.value = String(sim.infection_model.beta);
  paint();
  input.oninput = () => {
    sim.infection_model.beta = normalizeBeta(input.value, sim.infection_model.beta);
    paint();
  };
}

// ── E/P/I 지속 시간 분포 에디터 ──────────────────────────────────────────────────
// 세 구간이 같은 DurationSpec 모양이라 한 컴포넌트(_durationBlockHtml)를 세 번 그린다.
// 입력은 즉시(oninput) 상태에 반영하고, 포커스를 벗어날 때(change) normalizeDurationSpec
// 으로 정규화해 다시 그린다 — 증상 단계 에디터와 같은 규칙(타이핑 중 커서 보존).
const DURATION_SLOTS = [
  { key: 'exposed_duration',        title: '잠복기 (E)',        sub: '비전염 · 노출 직후',
    def: DEFAULT_EXPOSED_DURATION },
  { key: 'presymptomatic_duration', title: '무증상 전염기 (P)', sub: '전염 가능 · 아직 증상 없음 · 0~0일이면 건너뜀',
    def: DEFAULT_PRESYMPTOMATIC_DURATION },
  { key: 'infectious_duration',     title: '감염기 (I)',        sub: '전염 가능 · 증상 있음',
    def: DEFAULT_INFECTIOUS_DURATION },
];
const DURATION_KIND_LABELS = { uniform: '균등분포', gamma: '감마분포', gaussian: '정규분포(가우시안)' };
const DURATION_NUM_FIELDS  = ['shape', 'scale', 'mean', 'stddev', 'min_days', 'max_days'];

function _durNum(slot, field, value, label, attrs = '') {
  return `
    <label class="sim-inf-dur-field">
      <span class="sim-inf-stage-tag">${label}</span>
      <input type="number" class="sim-inf-stage-num sim-inf-dur-num" step="any" ${attrs}
             data-slot="${slot}" data-field="${field}" value="${esc(String(value))}"/>
    </label>`;
}

/** 분포 설정 한 벌(E/P/I 공통). kind 에 따라 파라미터 칸을 조건부로 보인다. */
function _durationBlockHtml(slot, spec) {
  const kindOpts = Object.entries(DURATION_KIND_LABELS)
    .map(([v, l]) => `<option value="${v}" ${spec.kind === v ? 'selected' : ''}>${l}</option>`).join('');
  return `
    <div class="sim-inf-dur-title">${esc(slot.title)} <span class="sim-inf-dur-sub">${esc(slot.sub)}</span></div>
    <div class="sim-inf-dur-row">
      <select class="sim-inf-dur-kind" data-slot="${slot.key}" data-field="kind">${kindOpts}</select>
      <span class="sim-inf-dur-params${spec.kind === 'gamma' ? '' : ' sim-hidden'}" data-kind-only="gamma">
        ${_durNum(slot.key, 'shape', spec.shape, 'k(형상)', 'min="0"')}
        ${_durNum(slot.key, 'scale', spec.scale, 'θ(척도)', 'min="0"')}
      </span>
      <span class="sim-inf-dur-params${spec.kind === 'gaussian' ? '' : ' sim-hidden'}" data-kind-only="gaussian">
        ${_durNum(slot.key, 'mean',   spec.mean,   'μ(평균)')}
        ${_durNum(slot.key, 'stddev', spec.stddev, 'σ(표준편차)', 'min="0"')}
      </span>
    </div>
    <div class="sim-inf-dur-row">
      ${_durNum(slot.key, 'min_days', spec.min_days, '최소', 'min="0"')}
      <span class="sim-inf-stage-sep">~</span>
      ${_durNum(slot.key, 'max_days', spec.max_days, '최대', 'min="0"')}
      <span class="sim-inf-stage-unit">일 (절단 범위 · 결과는 일 단위 반올림)</span>
    </div>
    <div class="sim-inf-stage-hint sim-inf-dur-hint"></div>`;
}

/** 블록 아래 요약 + "조용히 경계값에 몰리는" 설정 경고. */
function _paintDurationHint(block, spec) {
  const hintEl = block?.querySelector('.sim-inf-dur-hint');
  if (!hintEl || !spec) return;
  const lo = spec.min_days, hi = spec.max_days;
  const warns = [];
  let summary;
  if (lo === hi) {
    summary = lo === 0 ? '이 구간 없음 (0일)' : `${lo}일 고정 (분포 종류와 무관)`;
  } else if (spec.kind === 'gamma') {
    const mean = Math.round(spec.shape * spec.scale * 100) / 100;
    summary = `절단 감마 — 원 분포 평균 k×θ = ${mean}일, ${lo}~${hi}일 밖의 표본은 재추첨`;
    if (mean < lo || mean > hi) warns.push('원 분포 평균이 절단 범위 밖이라 대부분 재추첨되고, 실패하면 경계값으로 몰립니다');
  } else if (spec.kind === 'gaussian') {
    summary = `절단 정규 — μ=${spec.mean}일, σ=${spec.stddev}일, ${lo}~${hi}일 밖의 표본은 재추첨`;
    if (spec.mean < lo || spec.mean > hi) warns.push('평균이 절단 범위 밖이라 대부분 재추첨되고, 실패하면 경계값으로 몰립니다');
  } else {
    summary = `${lo}~${hi}일 균등`;
  }
  if (hi < lo) warns.push('최대가 최소보다 작습니다 — 입력을 마치면 최소가 최대에 맞춰 조정됩니다');
  hintEl.classList.toggle('sim-inf-stage-hint-warn', warns.length > 0);
  hintEl.textContent = [summary, ...warns.map(w => `⚠ ${w}`)].join(' · ');
}

/** 모델 select(sir/seir/sepir) — 값은 즉시 상태에 반영되고 숨긴 구간은 0일로 강제된다. */
function _bindModelType() {
  const el = document.getElementById('sim-inf-model-type');
  if (!el) return;
  el.value = sim.infection_model.model_type;
  el.onchange = () => setModelType(el.value);
}

/**
 * 모델 종류 변경. 숨겨지는 구간(SIR: E·P / SEIR: P)의 지속을 0~0일로 강제한 뒤 편집기를
 * 다시 그린다 — 화면엔 안 보이는데 예전 P>0이 남아 계속 동작하는 모순을 막는다.
 * 되돌려 SEPIR로 바꿔도 이전 값은 복원하지 않는다(0일에서 다시 시작).
 */
export function setModelType(modelType) {
  sim.infection_model.model_type = MODEL_TYPES.includes(modelType) ? modelType : 'sepir';
  applyModelType(sim.infection_model);
  _renderDurationEditors();
  _renderSymptomStages();
}

function _renderDurationEditors() {
  const container = document.getElementById('sim-inf-durations');
  if (!container) return;
  container.innerHTML = '';
  const visible = visibleDurationKeys(sim.infection_model.model_type);
  DURATION_SLOTS.forEach(slot => {
    const spec  = sim.infection_model[slot.key];
    const block = document.createElement('div');
    // 숨긴 구간도 DOM에는 두고 감춘다(값은 applyModelType이 0일로 고정해 둔다).
    block.className = `sim-inf-dur-block${visible.includes(slot.key) ? '' : ' sim-hidden'}`;
    block.dataset.slot = slot.key;
    block.innerHTML = _durationBlockHtml(slot, spec);
    container.appendChild(block);
    _paintDurationHint(block, spec);
  });

  // renderInfectionConfig()가 여러 번 불려도 리스너가 쌓이지 않도록 프로퍼티로 덮어쓴다.
  container.oninput = e => {
    const el   = e.target;
    const spec = sim.infection_model[el.dataset?.slot];
    if (!spec || !DURATION_NUM_FIELDS.includes(el.dataset.field)) return;
    const n = parseFloat(el.value);
    if (Number.isFinite(n)) spec[el.dataset.field] = n;   // 지우는 도중(빈 칸)은 무시
    _paintDurationHint(el.closest('.sim-inf-dur-block'), spec);
  };
  container.onchange = e => {
    const el   = e.target;
    const slot = DURATION_SLOTS.find(s => s.key === el.dataset?.slot);
    if (!slot) return;
    if (el.dataset.field === 'kind') sim.infection_model[slot.key].kind = el.value;
    sim.infection_model[slot.key] = normalizeDurationSpec(sim.infection_model[slot.key], slot.def);
    _renderDurationEditors();
    _renderSymptomStages();   // 0일 ↔ 양수 전환이 "이 상태를 건너뜀" 안내를 바꾼다
  };
}

// ── 환자 0번 피커 ─────────────────────────────────────────────────────────────
// 별도 상태를 두지 않는다. 이 피커는 sim.events의 infect_agent 이벤트를 그대로 보여주고
// 고칠 뿐이라, 아래쪽 "시나리오 이벤트" 편집기와 언제나 같은 데이터를 본다.
//   체크됨  = 그 에이전트를 가리키는 infect_agent 이벤트가 하나 이상 있음
//   체크    = { type:'infect_agent', agent, wave:<발병 시점>, start_status:<시작 상태>, message:'' } 추가
//   체크 해제 = 그 에이전트의 infect_agent 이벤트 전부 제거
// 피커가 이벤트를 건드리면 renderScenarioEvents()로 편집기를 다시 그려 둘을 맞춘다.
const INFECT_EVENT = 'infect_agent';

function _infectEvents() {
  return (sim.events || []).filter(e => e && e.type === INFECT_EVENT);
}

/**
 * 이미 삭제된 에이전트를 가리키는 infect_agent 이벤트를 조용히 제거한다.
 * events.js의 _syncAgentSelection()은 stale ref를 sim.agents[0]으로 몰래 재지정하는데,
 * 감염 시드에서 그러면 사용자가 고른 적 없는 사람이 환자 0번이 된다.
 * 단 에이전트 목록이 아직 비어 있는 순간(시나리오 로드 중 등)에는 아무것도 지우지 않는다 —
 * 그 판단으로는 "삭제됨"과 "아직 안 채워짐"을 구분할 수 없기 때문.
 */
function _pruneStaleInfectEvents() {
  if (!sim.agents.length || !Array.isArray(sim.events)) return false;
  const known = new Set(sim.agents.map(a => a.name));
  let removed = false;
  for (let i = sim.events.length - 1; i >= 0; i--) {
    const ev = sim.events[i];
    if (ev?.type === INFECT_EVENT && !known.has(ev.agent)) {
      sim.events.splice(i, 1);
      removed = true;
    }
  }
  return removed;
}

/** "발병 시점" 입력의 현재 값(0~99). 비어 있거나("혼합") 잘못된 값이면 0. */
function _readOnsetWave() {
  const el = document.getElementById('sim-inf-onset-wave');
  const n  = parseInt(el?.value);
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(99, n));
}

/** infect_agent 이벤트들의 wave를 입력란에 반영. 값이 섞여 있으면 빈 칸 + "혼합". */
function _paintOnsetWave() {
  const el = document.getElementById('sim-inf-onset-wave');
  if (!el) return;
  const waves = [...new Set(_infectEvents().map(e => parseInt(e.wave) || 0))];
  if (waves.length === 1) {
    el.value = String(waves[0]);
    el.placeholder = '';
  } else if (waves.length > 1) {
    // 이벤트 편집기에서 개별 조정한 경우 — 사용자가 이 칸을 실제로 고치기 전에는
    // 마음대로 통일하지 않는다.
    el.value = '';
    el.placeholder = '혼합';
  } else {
    // 아직 환자 0번이 없다 — 사용자가 미리 적어둔 값을 그대로 둔다(다음 체크에 쓰인다).
    el.placeholder = '0';
  }
}

/** 환자 0번 칩 목록 + 발병 시점 입력을 sim.events 기준으로 다시 그린다. */
export function renderPatientZeroPicker() {
  const chipsEl = document.getElementById('sim-inf-patient-zero');
  if (!chipsEl) return;
  _pruneStaleInfectEvents();

  const seeded = new Set(_infectEvents().map(e => e.agent));
  chipsEl.innerHTML = '';

  if (!sim.agents.length) {
    const empty = document.createElement('span');
    empty.className = 'sim-inf-pz-empty';
    empty.textContent = '에이전트가 없습니다 — 먼저 “에이전트” 섹션에서 추가하세요.';
    chipsEl.appendChild(empty);
  }

  sim.agents.forEach(agent => {
    const on   = seeded.has(agent.name);
    const chip = document.createElement('span');
    chip.className = `evt-target-chip sim-inf-pz-chip${on ? ' selected' : ''}`;
    chip.dataset.agent = agent.name;
    chip.setAttribute('role', 'checkbox');
    chip.setAttribute('aria-checked', on ? 'true' : 'false');
    // 사용자 입력(이름/아이콘)이 그대로 들어오므로 textContent로만 넣는다.
    chip.textContent = `${on ? '☑' : '☐'} ${getAgentIcon(agent, 'neutral')} ${agent.display_name || agent.name}`;
    chip.onclick = () => _togglePatientZero(agent.name);
    chipsEl.appendChild(chip);
  });

  _paintOnsetWave();
  _bindOnsetWave();
  _paintStartStatus();
  _bindStartStatus();
  updatePatientZeroWarning();
}

// ── 환자 0번 시작 상태 (E/P/I) ──────────────────────────────────────────────────
// 발병 시점 입력과 같은 규칙: 이 select 는 모든 infect_agent 이벤트의 start_status 를
// 보여주고 한꺼번에 고친다. 값이 섞여 있으면(이벤트 편집기에서 개별 조정) "혼합"을
// 보이고, 사용자가 실제로 고르기 전에는 통일하지 않는다.
function _readStartStatus() {
  const v = document.getElementById('sim-inf-start-status')?.value;
  return INFECT_START_STATUS.some(([k]) => k === v) ? v : 'E';
}

function _paintStartStatus() {
  const el = document.getElementById('sim-inf-start-status');
  if (!el) return;
  const statuses = [...new Set(_infectEvents().map(e => e.start_status || 'E'))];
  const mixedOpt = el.querySelector('option[value=""]');
  if (statuses.length > 1) {
    if (!mixedOpt) el.insertAdjacentHTML('afterbegin', '<option value="">혼합</option>');
    el.value = '';
  } else {
    mixedOpt?.remove();
    // 환자 0번이 아직 없으면 사용자가 미리 고른 값을 그대로 둔다(다음 체크에 쓰인다).
    if (statuses.length === 1) el.value = statuses[0];
  }
}

function _bindStartStatus() {
  const el = document.getElementById('sim-inf-start-status');
  if (!el) return;
  el.onchange = () => {
    if (!el.value) return;                 // "혼합" 재선택 — 아무것도 바꾸지 않음
    const v = _readStartStatus();
    let changed = false;
    _infectEvents().forEach(e => { if ((e.start_status || 'E') !== v) { e.start_status = v; changed = true; } });
    _paintStartStatus();
    if (changed) _syncEventsEditor();
  };
}

/** 칩 클릭 — 그 에이전트의 infect_agent 이벤트를 만들거나 전부 지운다. */
function _togglePatientZero(name) {
  if (!Array.isArray(sim.events)) sim.events = [];
  const has = sim.events.some(e => e?.type === INFECT_EVENT && e.agent === name);
  if (has) {
    for (let i = sim.events.length - 1; i >= 0; i--) {
      const ev = sim.events[i];
      if (ev?.type === INFECT_EVENT && ev.agent === name) sim.events.splice(i, 1);
    }
  } else {
    // 중복 방지는 위 has 검사가 담당한다(이미 있으면 추가하지 않고 해제로 간다).
    // targets는 감염 시드에서 쓰이지 않아 서버 기본값(["all"])에 맡긴다.
    sim.events.push({ type: INFECT_EVENT, agent: name, wave: _readOnsetWave(),
                      start_status: _readStartStatus(), message: '' });
  }
  renderPatientZeroPicker();
  _syncEventsEditor();
}

// 발병 시점 입력은 renderPatientZeroPicker()가 여러 번 불려도 리스너가 쌓이지 않도록
// addEventListener 대신 .oninput/.onchange 프로퍼티로 덮어쓴다(이 파일의 다른 입력과 같은 규칙).
function _bindOnsetWave() {
  const el = document.getElementById('sim-inf-onset-wave');
  if (!el) return;
  el.oninput = () => {
    // 지우는 도중(빈 칸)에는 아무것도 하지 않는다 — 타이핑 중에 모든 wave가 0으로
    // 몰리는 일을 막는다. 확정(change)에서 정리한다.
    if (el.value.trim() === '') return;
    _applyOnsetWave(_readOnsetWave());
  };
  el.onchange = () => {
    if (el.value.trim() === '') { _paintOnsetWave(); return; }   // 편집 취소로 본다
    const w = _readOnsetWave();
    el.value = String(w);        // 범위 밖 입력(예: 200)을 클램프 결과로 되쓴다
    _applyOnsetWave(w);
  };
}

/** 모든 infect_agent 이벤트의 wave를 하나로 통일. 사용자가 입력을 실제로 고쳤을 때만 호출된다. */
function _applyOnsetWave(wave) {
  const evs = _infectEvents();
  if (!evs.length) return;
  let changed = false;
  evs.forEach(e => { if (e.wave !== wave) { e.wave = wave; changed = true; } });
  if (changed) _syncEventsEditor();
}

/** 이벤트 편집기 재렌더 — 그쪽 DOM이 없는 경로(설정 패널 미렌더)에서는 건너뛴다. */
function _syncEventsEditor() {
  if (document.getElementById('sim-events-list')) renderScenarioEvents();
}

/**
 * 모델을 켜 놓고 환자 0번을 아무도 지정하지 않으면 감염이 영원히 시작되지 않는다
 * (엔진은 감염자 0명에서 아무 전염도 굴리지 않는다). 시간 경고와 별개 요소라 동시에 뜰 수 있다.
 */
export function updatePatientZeroWarning() {
  const warnEl = document.getElementById('sim-inf-pz-warn');
  if (!warnEl) return;
  // 체크박스가 상태보다 앞선 순간(사용자가 방금 켠 직후)이 있으므로 폼을 먼저 본다 —
  // updateInfectionTimeWarning()이 시간 입력을 폼에서 읽는 것과 같은 규칙.
  const chk     = document.getElementById('sim-inf-enabled');
  const enabled = chk ? chk.checked : !!sim.infection_model?.enabled;
  warnEl.classList.toggle('sim-hidden', !(enabled && _infectEvents().length === 0));
}

// ── 시간 개념 경고 ────────────────────────────────────────────────────────────
// time_mode='fixed' + wave당 시간 0이면 경과 분이 영원히 0이라 증상 단계가 진행되지도
// 자연 회복이 일어나지도 않는다(전염은 wave·접촉 기준이라 정상 동작). 버그가 아니라
// 시간 기준 모델의 정의상 결과지만, 사용자에게는 "설정이 먹히지 않는" 것으로 보인다.
export function updateInfectionTimeWarning() {
  const warnEl = document.getElementById('sim-inf-time-warn');
  if (!warnEl) return;
  // 시간 모드/wave당 시간은 폼에서 실시간으로 읽는다 — 아직 sim에 반영되기 전일 수 있다
  // (updateTargetDurationUI와 같은 규칙).
  const modeEl = document.getElementById('sim-time-mode');
  const tpwEl  = document.getElementById('sim-time-per-wave');
  const mode = modeEl ? (modeEl.value === 'variable' ? 'variable' : 'fixed') : sim.time_mode;
  const tpw  = tpwEl  ? (parseInt(tpwEl.value) || 0)                        : sim.time_per_wave;
  const off  = isTimeConceptDisabled(mode, tpw);
  warnEl.classList.toggle('sim-hidden', !off);
}

// 시간 설정 입력은 감염 섹션 바깥(세계 설정)에 있고 renderInfectionConfig()는 여러 번
// 호출되므로, 리스너가 쌓이지 않도록 한 번만 붙인다. 해당 요소들은 이미 다른 모듈이
// .onchange 프로퍼티를 쓰고 있어(initTimeModeToggle) addEventListener로 공존시킨다.
let _timeWatchersBound = false;
function _bindTimeWatchers() {
  if (_timeWatchersBound) return;
  const tpwEl  = document.getElementById('sim-time-per-wave');
  const modeEl = document.getElementById('sim-time-mode');
  if (!tpwEl && !modeEl) return;
  tpwEl?.addEventListener('input',   updateInfectionTimeWarning);
  modeEl?.addEventListener('change', updateInfectionTimeWarning);
  _timeWatchersBound = true;
}

// ── 증상 문구 에디터 ──────────────────────────────────────────────────────────
// 시간창(min/max) 없이 "상태(E/P/I) + 그 상태 안의 순서"만 편집한다. 상태별로 묶어 보여주고
// (normalizeSymptomStages가 E→P→I로 안정 정렬하므로 같은 상태 항목은 배열에서도 붙어 있다),
// 묶음 안에서 ↑/↓로 순서를 바꾼다 — 위에서 아래 순서가 곧 그 상태 안의 진행 순서다.
const STATUS_ICONS = { E: '⏳', P: '😶', I: '🦠' };
const STATUS_DURATION_KEY = { E: 'exposed_duration', P: 'presymptomatic_duration', I: 'infectious_duration' };

function _renderSymptomStages() {
  const container = document.getElementById('sim-inf-stages');
  if (!container) return;
  sim.infection_model.symptom_stages = normalizeSymptomStages(sim.infection_model.symptom_stages);
  const stages  = sim.infection_model.symptom_stages;
  const visible = visibleDurationKeys(sim.infection_model.model_type);
  container.innerHTML = '';

  SYMPTOM_STATUSES.forEach(status => {
    const members = stages.map((st, idx) => ({ st, idx })).filter(m => m.st.status === status);
    const hidden  = !visible.includes(STATUS_DURATION_KEY[status]);
    // 모델이 숨긴 구간뿐 아니라, SEPIR이라도 지속이 0일 고정이면 엔진이 이 상태를 건너뛴다
    // (기본 P = 0일). 그때 "이 상태 내내 이 문구를 씁니다"라고 안내하면 거짓말이 된다.
    const skipped = isStatusAlwaysSkipped(sim.infection_model, status);
    const group = document.createElement('div');
    group.className = 'sim-inf-sym-group';
    group.dataset.status = status;
    group.dataset.skipped = skipped ? '1' : '';
    const note = hidden
      ? '현재 모델은 이 상태를 건너뛰므로 이 문구는 쓰이지 않습니다'
      : skipped
        ? '지속 시간이 0일이라 이 상태를 건너뜁니다 — 문구를 쓰려면 위 지속 시간 분포에서 최대를 1일 이상으로 늘리세요'
      : members.length
        ? (members.length > 1 ? `이 상태의 진행 구간을 ${members.length}등분해 위에서부터 순서대로 씁니다` : '이 상태 내내 이 문구를 씁니다')
        : '문구가 없으면 이 상태 동안 몸 상태가 전달되지 않습니다';
    group.innerHTML = `
      <div class="sim-inf-sym-head">
        <span class="sim-inf-sym-title">${STATUS_ICONS[status]} ${esc(SYMPTOM_STATUS_LABELS[status])}</span>
        <span class="sim-inf-sym-note">${esc(note)}</span>
        <button class="sim-settings-add-btn sim-inf-sym-add" data-add="${status}">+ 추가</button>
      </div>
      ${members.map(({ st, idx }, k) => `
      <div class="sim-inf-stage-row" data-idx="${idx}">
        <div class="sim-inf-stage-top">
          <select class="sim-inf-sym-status" data-idx="${idx}" title="이 문구가 쓰일 상태">
            ${SYMPTOM_STATUSES.map(v => `<option value="${v}" ${v === st.status ? 'selected' : ''}>${STATUS_ICONS[v]} ${esc(SYMPTOM_STATUS_LABELS[v])}</option>`).join('')}
          </select>
          <span class="sim-inf-sym-order">${k + 1}/${members.length}</span>
          <button class="sim-inf-sym-move" data-idx="${idx}" data-dir="-1" title="위로" ${k === 0 ? 'disabled' : ''}>↑</button>
          <button class="sim-inf-sym-move" data-idx="${idx}" data-dir="1"  title="아래로" ${k === members.length - 1 ? 'disabled' : ''}>↓</button>
          <button class="sim-inf-stage-del" data-idx="${idx}" title="문구 삭제">×</button>
        </div>
        <textarea class="sim-inf-stage-text" data-idx="${idx}" rows="2"
                  placeholder="이 상태에서 에이전트가 느끼는 몸 상태를 서술하세요. 이 문장이 LLM에게 전달되는 유일한 정보입니다.">${esc(st.symptom_text)}</textarea>
      </div>`).join('')}`;
    container.appendChild(group);
  });

  // 텍스트는 즉시 반영한다 — 다시 그릴 때 편집 중이던 값이 날아가지 않게.
  container.oninput = e => {
    const el = e.target;
    if (!el.classList?.contains('sim-inf-stage-text')) return;
    const stage = sim.infection_model.symptom_stages[parseInt(el.dataset.idx)];
    if (stage) stage.symptom_text = el.value;
  };
  container.onchange = e => {
    const el = e.target;
    if (!el.classList?.contains('sim-inf-sym-status')) return;
    const stage = sim.infection_model.symptom_stages[parseInt(el.dataset.idx)];
    if (!stage) return;
    // 새 상태 묶음의 맨 끝으로 간다(배열 뒤로 옮긴 뒤 안정 정렬).
    const list = sim.infection_model.symptom_stages;
    list.splice(parseInt(el.dataset.idx), 1);
    list.push({ ...stage, status: el.value });
    _renderSymptomStages();
  };
  container.onclick = e => {
    const add = e.target.closest?.('.sim-inf-sym-add');
    if (add) { addSymptomStage(add.dataset.add); return; }
    const del = e.target.closest?.('.sim-inf-stage-del');
    if (del) {
      sim.infection_model.symptom_stages.splice(parseInt(del.dataset.idx), 1);
      _renderSymptomStages();
      return;
    }
    const mv = e.target.closest?.('.sim-inf-sym-move');
    if (mv) moveSymptomStage(parseInt(mv.dataset.idx), parseInt(mv.dataset.dir));
  };
}

/** 같은 상태 묶음 안에서 한 칸 이동(dir=-1 위, +1 아래). 묶음 경계를 넘지 않는다. */
export function moveSymptomStage(idx, dir) {
  const list = sim.infection_model.symptom_stages;
  const j = idx + dir;
  if (!list[idx] || !list[j] || list[j].status !== list[idx].status) return;
  [list[idx], list[j]] = [list[j], list[idx]];
  _renderSymptomStages();
}

/**
 * 문구 추가 — 지정한 상태 묶음의 끝(= 그 상태의 가장 늦은 진행 구간)에 붙인다.
 * 상단 "+ 문구 추가" 버튼은 인자 없이 불리며 감염기(I)에 추가한다.
 */
export function addSymptomStage(status) {
  sim.infection_model = buildInfectionModel(sim.infection_model);
  const st = SYMPTOM_STATUSES.includes(status) ? status : 'I';
  sim.infection_model.symptom_stages.push({ status: st, symptom_text: '' });
  _renderSymptomStages();
}

/** 폼 → infection_model. 설정 패널이 렌더되지 않은 경로에서는 기존 상태를 유지한다. */
export function readInfectionModel() {
  const chk = document.getElementById('sim-inf-enabled');
  if (!chk) return buildInfectionModel(sim.infection_model);
  return buildInfectionModel({
    enabled:                  chk.checked,
    disease_name:             document.getElementById('sim-inf-disease-name')?.value ?? '',
    beta:                     document.getElementById('sim-inf-beta')?.value,
    // 편집기가 비어 있으면 빈 배열 그대로 — 백엔드도 빈 목록을 허용한다(증상 없음).
    symptom_stages:           sim.infection_model?.symptom_stages ?? [],
    immune_after_recovery:    document.getElementById('sim-inf-immune')?.value !== 'sis',
    model_type:               sim.infection_model?.model_type,
    // 분포 에디터는 입력 즉시 sim.infection_model 에 반영하므로 상태에서 읽는다.
    exposed_duration:         sim.infection_model?.exposed_duration,
    presymptomatic_duration:  sim.infection_model?.presymptomatic_duration,
    infectious_duration:      sim.infection_model?.infectious_duration,
  });
}
