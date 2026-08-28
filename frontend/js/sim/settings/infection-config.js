// frontend/js/sim/settings/infection-config.js
// 감염병 모델 설정 — 폼 ↔ sim.infection_model 동기화. 서버 검증(전염 확률 0~1,
// 모든 분 값 0~52560000, max >= min)을 통과하도록 읽어들이는 모든 경로가
// buildInfectionModel()을 거친다.
//
// 단위 규칙: 전염만 wave·접촉 기준이고(슬라이더 유지), 증상 단계 진행과 회복은
// "감염 후 경과 분" 기준이다. 사람이 쓰기 편하도록 (일 + 시간) 두 칸으로 입력받고
// dayHourToMinutes()로 분으로 환산해 저장한다.

import { sim, esc, buildInfectionModel, normalizeProbability,
         normalizeSymptomStages, dayHourToMinutes, minutesToDayHour,
         formatDayHour, isTimeConceptDisabled } from '../state.js';
import { renderScenarioEvents } from './events.js';

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

  _bindProbabilitySlider('sim-inf-transmission', 'transmission_probability');
  _renderRecoveryWindow();
  _renderSymptomStages();
  updateInfectionTimeWarning();
  _bindTimeWatchers();

  // renderSettingsPage()는 여러 번 호출되므로 addEventListener 대신 onchange로 덮어쓴다
  // (renderSystemAgentConfig / renderServerSelect와 같은 규칙 — 리스너 중복 등록 방지).
  chk.onchange = () => {
    sim.infection_model.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
    // infect_agent 이벤트는 모델이 꺼져 있으면 서버에서 무시된다 —
    // 이벤트 에디터의 경고 배지를 지금 상태에 맞춰 다시 그린다.
    renderScenarioEvents();
  };
  if (nameEl)   nameEl.oninput    = () => { sim.infection_model.disease_name = nameEl.value.trim(); };
  if (immuneEl) immuneEl.onchange = () => { sim.infection_model.immune_after_recovery = immuneEl.value !== 'sis'; };
}

/** 확률 슬라이더 + 현재 값 표시. 값은 즉시 상태에 반영된다(temperature 슬라이더와 동일). */
function _bindProbabilitySlider(id, field) {
  const slider = document.getElementById(id);
  const valEl  = document.getElementById(`${id}-val`);
  if (!slider) return;
  const paint = () => { if (valEl) valEl.textContent = sim.infection_model[field].toFixed(2); };
  slider.value = String(sim.infection_model[field]);
  paint();
  slider.oninput = () => {
    sim.infection_model[field] = normalizeProbability(slider.value, sim.infection_model[field]);
    paint();
  };
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

// ── 회복 시간 ─────────────────────────────────────────────────────────────────
// 감염 시점에 [min, max]분에서 균등 샘플된 값이 확정되고, 경과 분이 그 값에 닿으면 회복한다.
// max === 0은 "자연 회복 없음(만성)"이라는 별도 의미라 백엔드도 min과 비교하지 않는다.
function _renderRecoveryWindow() {
  const fieldOf = bound => (bound === 'min' ? 'recovery_min_minutes' : 'recovery_max_minutes');
  ['min', 'max'].forEach(bound => {
    const { days, hours } = minutesToDayHour(sim.infection_model[fieldOf(bound)]);
    const dEl = document.getElementById(`sim-inf-recovery-${bound}-d`);
    const hEl = document.getElementById(`sim-inf-recovery-${bound}-h`);
    if (dEl) dEl.value = String(days);
    if (hEl) hEl.value = String(hours);
    if (!dEl || !hEl) return;
    const sync = () => {
      const mins = dayHourToMinutes(dEl.value, hEl.value);
      // sim.infection_model을 캡처하지 말 것 — addSymptomStage()와 readConfigFromUI()가
      // 이 객체를 통째로 새 것으로 갈아끼우므로(buildInfectionModel), 캡처하면 핸들러가
      // 고아가 된 옛 객체에 쓰게 되고 블러 전에 시작을 누르면 화면과 다른 값이 전송된다.
      // 증상 단계 쪽 container.oninput과 같은 규칙으로 매번 live 조회한다.
      sim.infection_model[fieldOf(bound)] = mins;
      _paintRecoveryHint();
    };
    // 입력은 즉시 상태에 반영하고(타이핑 중 커서를 잃지 않게 다시 그리지 않는다),
    // 포커스를 벗어날 때만 정규화 결과로 화면을 맞춘다 — 증상 단계와 같은 규칙.
    dEl.oninput = sync;
    hEl.oninput = sync;
    dEl.onchange = hEl.onchange = () => {
      sim.infection_model = buildInfectionModel(sim.infection_model);
      _renderRecoveryWindow();
    };
  });
  _paintRecoveryHint();
}

function _paintRecoveryHint() {
  const hintEl = document.getElementById('sim-inf-recovery-hint');
  if (!hintEl) return;
  const { recovery_min_minutes: min, recovery_max_minutes: max } = sim.infection_model;
  hintEl.classList.toggle('sim-inf-chronic', max === 0);
  if (max === 0) {
    hintEl.textContent = '자연 회복 없음 — 한 번 감염되면 스스로 낫지 않습니다(만성).';
    return;
  }
  hintEl.textContent = min === max
    ? `감염 후 정확히 ${formatDayHour(max)} 뒤에 회복합니다.`
    : `감염 시점에 ${formatDayHour(min)} ~ ${formatDayHour(max)} 사이에서 무작위로 정해집니다.`;
}

// ── 증상 단계 에디터 ──────────────────────────────────────────────────────────
// time_categories 에디터와 같은 구조(id/label/min/max)에 symptom_text textarea를 더한
// 형태. 단위는 웨이브가 아니라 **감염 후 경과 시간**이고, 일 + 시간 두 칸으로 입력받는다.
function _renderSymptomStages() {
  const container = document.getElementById('sim-inf-stages');
  if (!container) return;
  const stages = sim.infection_model.symptom_stages;
  container.innerHTML = '';

  if (!stages.length) {
    const empty = document.createElement('div');
    empty.className = 'sim-inf-stage-empty';
    empty.textContent = '증상 단계가 없습니다 — 단계를 추가해야 에이전트가 자기 몸 상태를 인지합니다.';
    container.appendChild(empty);
  }

  stages.forEach((stage, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-inf-stage-row';
    row.dataset.stageId = stage.id;
    row.innerHTML = `
      <div class="sim-inf-stage-top">
        <input type="text" class="sim-inf-stage-label" data-idx="${idx}"
               value="${esc(stage.label)}" placeholder="단계 이름"/>
        <button class="sim-inf-stage-del" data-idx="${idx}" title="단계 삭제">×</button>
      </div>
      <div class="sim-inf-stage-range">
        ${_boundInputs(idx, 'min', stage.min_minutes, '시작')}
        <span class="sim-inf-stage-sep">~</span>
        ${_boundInputs(idx, 'max', stage.max_minutes, '끝')}
      </div>
      <div class="sim-inf-stage-hint"></div>
      <textarea class="sim-inf-stage-text" data-idx="${idx}" rows="2"
                placeholder="이 단계에서 에이전트가 느끼는 몸 상태를 서술하세요. 이 문장이 LLM에게 전달되는 유일한 정보입니다.">${esc(stage.symptom_text)}</textarea>`;
    container.appendChild(row);
    _paintStageHint(row, stage, idx);
  });

  // 입력은 즉시 상태에 반영한다 — 삭제/추가로 다시 그릴 때 편집 중이던 값이 날아가지 않게.
  container.oninput = e => {
    const el  = e.target;
    const idx = parseInt(el.dataset?.idx);
    const stage = sim.infection_model.symptom_stages[idx];
    if (!stage) return;
    if (el.classList.contains('sim-inf-stage-label'))     stage.label        = el.value;
    else if (el.classList.contains('sim-inf-stage-text')) stage.symptom_text = el.value;
    else if (el.dataset.bound) {
      const row   = el.closest('.sim-inf-stage-row');
      const bound = el.dataset.bound;
      const dEl = row?.querySelector(`[data-bound="${bound}"][data-part="d"]`);
      const hEl = row?.querySelector(`[data-bound="${bound}"][data-part="h"]`);
      const mins = dayHourToMinutes(dEl?.value, hEl?.value);
      if (bound === 'min') stage.min_minutes = mins;
      else                 stage.max_minutes = mins;
      _paintAllStageHints();   // 이 단계의 끝을 줄이면 다음 단계에 공백 경고가 생긴다
    }
  };
  // 일/시간은 포커스를 벗어날 때(change) 정규화해서 다시 그린다 — 그래야 사용자가
  // 방금 고친 값이 실제로 clamp된 뒤의 값과 화면에서 어긋나지 않는다(예: 30시간 → 1일 6시간).
  // 매 키 입력마다(oninput) 다시 그리면 타이핑 중간에 커서가 날아가므로 change 시점에만 한다.
  container.onchange = e => {
    if (!e.target.dataset?.bound) return;
    sim.infection_model.symptom_stages = normalizeSymptomStages(sim.infection_model.symptom_stages);
    _renderSymptomStages();
  };
  container.onclick = e => {
    const del = e.target.closest('.sim-inf-stage-del');
    if (!del) return;
    sim.infection_model.symptom_stages.splice(parseInt(del.dataset.idx), 1);
    _renderSymptomStages();
  };
}

/** "시작 [N]일 [M]시간" 한 벌. bound는 'min' | 'max'. */
function _boundInputs(idx, bound, minutes, tag) {
  const { days, hours } = minutesToDayHour(minutes);
  return `
    <span class="sim-inf-stage-tag">${tag}</span>
    <input type="number" class="sim-inf-stage-num" min="0" max="36500" step="1"
           data-idx="${idx}" data-bound="${bound}" data-part="d" value="${days}"/>
    <span class="sim-inf-stage-unit">일</span>
    <input type="number" class="sim-inf-stage-num" min="0" step="1"
           data-idx="${idx}" data-bound="${bound}" data-part="h" value="${hours}"/>
    <span class="sim-inf-stage-unit">시간</span>`;
}

/**
 * 행 아래 미리보기 — 실제 저장되는 분 값과 "증상이 조용히 안 나오는" 구간 이상을 표시.
 * 엔진 조회 규칙(min <= 경과분 <= max인 **첫** 단계, 정의된 최대를 넘으면 마지막 단계 유지)에서
 * 사용자가 눈치채기 어려운 세 가지를 잡는다: 뒤집힌 범위 / 길이 0 구간 / 단계 사이 공백.
 */
function _paintStageHint(row, stage, idx) {
  const hintEl = row?.querySelector('.sim-inf-stage-hint');
  if (!hintEl || !stage) return;
  const stages = sim.infection_model.symptom_stages;
  const warns  = [];

  if (stage.max_minutes < stage.min_minutes) {
    // 저장 시 normalizeSymptomStages()가 min을 max로 낮춘다(서버는 422로 거부한다).
    warns.push('끝이 시작보다 빠릅니다 — 입력을 마치면 시작이 끝에 맞춰 조정됩니다');
  } else if (stage.max_minutes === stage.min_minutes && stages.length > 1) {
    // 구버전 시나리오는 모든 단계가 0~0으로 리셋된다. 이때 엔진의 "최대 구간을 넘으면
    // 가장 늦은 단계 유지" 폴백이 동점에서 첫 원소를 고르므로, 1단계 증상만 영구히 나온다.
    warns.push('구간 길이가 0이라 이 단계는 사실상 진행하지 않습니다 — 끝을 다시 입력하세요');
  }

  if (idx === 0) {
    if (stage.min_minutes > 0) {
      warns.push('첫 단계가 0에서 시작하지 않아 그 전까지는 증상이 전달되지 않습니다');
    }
  } else {
    // 이전 단계의 끝과 이번 시작 사이가 비면 그 구간은 어느 단계에도 안 걸려 증상이 None이다
    // (마지막 단계를 넘긴 뒤와 달리 폴백이 없다). addSymptomStage는 틈을 안 만들지만
    // 사용자가 앞 단계의 "끝"을 줄이면 생긴다.
    const prev = stages[idx - 1];
    if (prev && stage.min_minutes > prev.max_minutes) {
      warns.push(`앞 단계 끝(${formatDayHour(prev.max_minutes)})과 이 단계 시작 사이가 비어 그 구간에는 증상이 전달되지 않습니다`);
    }
  }

  hintEl.classList.toggle('sim-inf-stage-hint-warn', warns.length > 0);
  hintEl.textContent = [
    `감염 후 ${formatDayHour(stage.min_minutes)} ~ ${formatDayHour(stage.max_minutes)}`,
    ...warns.map(w => `⚠ ${w}`),
  ].join(' · ');
}

/** 한 칸을 고치면 이웃 단계의 공백 경고도 달라지므로 행 전체의 힌트를 다시 칠한다. */
function _paintAllStageHints() {
  const rows = document.getElementById('sim-inf-stages')?.querySelectorAll('.sim-inf-stage-row') ?? [];
  rows.forEach((row, i) => _paintStageHint(row, sim.infection_model.symptom_stages[i], i));
}

/** "+ 단계 추가" 버튼 — 이전 단계가 끝나는 시각부터 시작하는 빈 단계를 붙인다(틈 방지). */
export function addSymptomStage() {
  sim.infection_model = buildInfectionModel(sim.infection_model);
  const stages = sim.infection_model.symptom_stages;
  const last   = stages[stages.length - 1];
  // 경계는 양끝을 포함하므로 한 분이 겹치지만, 엔진은 "첫 매치"를 쓰므로 앞 단계가 이긴다.
  const start  = last ? last.max_minutes : 0;
  stages.push({
    id:           `stage${stages.length + 1}`,
    label:        `단계 ${stages.length + 1}`,
    min_minutes:  start,
    max_minutes:  start + 2880,   // 기본 2일 구간
    symptom_text: '',
  });
  _renderSymptomStages();
}

/** 폼 → infection_model. 설정 패널이 렌더되지 않은 경로에서는 기존 상태를 유지한다. */
export function readInfectionModel() {
  const chk = document.getElementById('sim-inf-enabled');
  if (!chk) return buildInfectionModel(sim.infection_model);
  return buildInfectionModel({
    enabled:                  chk.checked,
    disease_name:             document.getElementById('sim-inf-disease-name')?.value ?? '',
    transmission_probability: document.getElementById('sim-inf-transmission')?.value,
    // 편집기가 비어 있으면 빈 배열 그대로 — 백엔드도 빈 목록을 허용한다(증상 없음).
    symptom_stages:           sim.infection_model?.symptom_stages ?? [],
    // 일/시간 두 칸짜리 입력은 편집 중에 이미 상태로 환산돼 들어와 있다 — 여기서 DOM을
    // 다시 읽으면 시간 미만 단위가 잘려나가므로(minutesToDayHour의 절삭) 상태를 쓴다.
    recovery_min_minutes:     sim.infection_model?.recovery_min_minutes,
    recovery_max_minutes:     sim.infection_model?.recovery_max_minutes,
    immune_after_recovery:    document.getElementById('sim-inf-immune')?.value !== 'sis',
  });
}
