// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).

import { sim, esc, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday,
         normalizeTemperature, DEFAULT_DURATION_UNIT, normalizeTargetDuration,
         minutesToDurationParts, durationPartsToMinutes, isTimeConceptDisabled,
         buildInfectionModel, normalizeProbability, normalizeSymptomStages } from '../state.js';
import { renderOutputFields } from './output-fields.js';
import { renderAgentListInConfig, renderStartAgentSelect, refreshAgentTempPlaceholders } from './agents.js';
import { renderScenarioEvents } from './events.js';
import { getServerList, invalidateServerList } from './server-list.js';

export function renderSettingsPage() {
  // 설정 패널을 열 때마다 서버 목록을 새로 읽는다 — 그 사이 서버 모달에서
  // 추가/삭제된 서버가 드롭다운에 반영되어야 하므로. 아래 시뮬레이션 레벨
  // 드롭다운과 에이전트별 드롭다운들이 이 한 번의 fetch를 공유한다.
  invalidateServerList();
  document.getElementById('sim-scenario-name').value  = sim.currentScenarioName;
  document.getElementById('sim-background').value     = sim.background;
  document.getElementById('sim-max-waves').value      = sim.max_waves;
  document.getElementById('sim-step-delay').value     = sim.step_delay;
  document.getElementById('sim-token-limit').value    = sim.token_limit;
  document.getElementById('sim-llm-max-tokens').value = sim.llm_max_tokens;
  document.getElementById('sim-output-format').value    = sim.output_format_template || '';
  document.getElementById('sim-summary-interval').value = sim.summary_interval ?? 0;
  const startTimeEl = document.getElementById('sim-start-time');
  if (startTimeEl) startTimeEl.value = sim.sim_start_time ?? '09:00';
  const startWeekdayEl = document.getElementById('sim-start-weekday');
  if (startWeekdayEl) startWeekdayEl.value = normalizeWeekday(sim.sim_start_weekday);
  const timePerWaveEl = document.getElementById('sim-time-per-wave');
  if (timePerWaveEl) timePerWaveEl.value = sim.time_per_wave ?? 30;
  const timeModeEl = document.getElementById('sim-time-mode');
  if (timeModeEl) {
    timeModeEl.value = sim.time_mode === 'variable' ? 'variable' : 'fixed';
    _updateVariableTimeUI(timeModeEl.value);
  }
  renderTargetDuration();
  _renderTimeCategories();
  const idleScheduleEl = document.getElementById('sim-idle-schedule');
  if (idleScheduleEl) {
    const sched = sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE;
    idleScheduleEl.value = sched.join(',');
  }
  const maxSilenceEl = document.getElementById('sim-max-silence-waves');
  if (maxSilenceEl) maxSilenceEl.value = sim.max_silence_waves ?? 3;
  const earlyStopEl = document.getElementById('sim-early-stop-enabled');
  if (earlyStopEl) {
    earlyStopEl.checked = sim.early_stop_enabled ?? true;
    _updateEarlyStopUI(earlyStopEl.checked);
  }
  const langFixEl = document.getElementById('sim-lang-fix-enabled');
  if (langFixEl) langFixEl.checked = sim.lang_fix_enabled ?? true;
  const langRetEl = document.getElementById('sim-lang-fix-retries');
  if (langRetEl) langRetEl.value = sim.lang_fix_retries ?? 2;
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = !sim.currentScenarioId;
  renderTemperatureSlider();
  renderOutputFields();
  renderAgentListInConfig();
  renderStartAgentSelect();
  renderScenarioEvents();
  renderLocationGraph();
  renderServerSelect();        // 비동기 — 드롭다운 별도 렌더링
  renderSystemAgentConfig();   // system 에이전트 설정 동기 렌더링
  renderInfectionConfig();     // 감염병 모델 설정
}

export function readConfigFromUI() {
  sim.background             = document.getElementById('sim-background').value.trim();
  sim.start_agent            = document.getElementById('sim-start-agent').value;
  sim.max_waves              = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.target_duration_minutes = _readTargetDuration();
  sim.step_delay             = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)     || 8192;
  sim.llm_max_tokens         = parseInt(document.getElementById('sim-llm-max-tokens').value) || 16384;
  sim.output_format_template = document.getElementById('sim-output-format').value;
  sim.summary_interval       = parseInt(document.getElementById('sim-summary-interval').value) || 0;
  sim.sim_start_time    = document.getElementById('sim-start-time')?.value || '09:00';
  sim.sim_start_weekday = normalizeWeekday(document.getElementById('sim-start-weekday')?.value);
  sim.time_per_wave     = parseInt(document.getElementById('sim-time-per-wave')?.value)     || 0;
  sim.time_mode         = document.getElementById('sim-time-mode')?.value === 'variable' ? 'variable' : 'fixed';
  sim.time_categories   = _readTimeCategories();
  sim.idle_minutes_schedule = _readIdleSchedule();
  sim.max_silence_waves  = parseInt(document.getElementById('sim-max-silence-waves')?.value)  || 3;
  sim.early_stop_enabled = document.getElementById('sim-early-stop-enabled')?.checked ?? true;
  const sel = document.getElementById('sim-server-select');
  sim.server_id              = sel?.value || null;
  // 슬라이더가 DOM에 있으면 그 값이 항상 우선이고(정규화는 범위 밖 대비),
  // 패널이 렌더되지 않은 상태로 호출되는 경로를 대비해 없을 때만 기존 상태로 폴백한다.
  const tempEl = document.getElementById('sim-temperature');
  sim.temperature = normalizeTemperature(tempEl ? tempEl.value : sim.temperature);
  sim.location_graph   = _readLocationGraph();
  sim.lang_fix_enabled = document.getElementById('sim-lang-fix-enabled')?.checked ?? true;
  sim.lang_fix_retries = parseInt(document.getElementById('sim-lang-fix-retries')?.value) || 2;
  // system 에이전트 설정 읽기
  sim.system_agent = {
    enabled:               document.getElementById('sim-sys-enabled')?.checked           ?? false,
    icon:                  document.getElementById('sim-sys-icon')?.value.trim()         || '🎬',
    display_name:          document.getElementById('sim-sys-display-name')?.value.trim() || '내레이터',
    system_prompt:         document.getElementById('sim-sys-prompt')?.value              || '',
    intervention_interval: parseInt(document.getElementById('sim-sys-interval')?.value)  || 1,
    silence_threshold:     parseInt(document.getElementById('sim-sys-silence')?.value)   || 3,
    director_note:         document.getElementById('sim-sys-director-note')?.value       || '',
  };
  sim.infection_model = _readInfectionModel();
}

// ── 조기 종료 토글 UI 연동 ────────────────────────────────────────────────────

function _updateEarlyStopUI(enabled) {
  const silenceInput = document.getElementById('sim-max-silence-waves');
  if (silenceInput) silenceInput.disabled = !enabled;
}

export function initEarlyStopToggle() {
  const chk = document.getElementById('sim-early-stop-enabled');
  if (!chk) return;
  chk.onchange = () => {
    sim.early_stop_enabled = chk.checked;
    _updateEarlyStopUI(chk.checked);
  };
}

// ── 목표 기간 (숫자 + 단위 ↔ target_duration_minutes) ─────────────────────────

// 마지막으로 폼에 그려 넣은 (숫자, 단위)와 그때의 원본 분 값.
// 저장된 분 값이 어떤 단위로도 딱 떨어지지 않으면 화면에는 일 단위 근사치를 보여주는데,
// 사용자가 입력을 건드리지 않았다면 이 원본 분 값을 그대로 되돌려줘야 한다.
// (근사치를 다시 저장하면 저장할 때마다 값이 조금씩 흘러가 버린다.)
let _renderedDuration = { value: '', unit: DEFAULT_DURATION_UNIT, minutes: null, exact: true };

function renderTargetDuration() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return;

  const minutes = normalizeTargetDuration(sim.target_duration_minutes);
  sim.target_duration_minutes = minutes;      // 구버전/잘못된 값(0, 음수, 문자열)을 상태에서도 바로잡는다
  const parts = minutesToDurationParts(minutes);
  valEl.value  = parts.value === '' ? '' : String(parts.value);
  unitEl.value = parts.unit;
  _renderedDuration = { value: valEl.value, unit: unitEl.value, minutes, exact: parts.exact };

  _updateTargetDurationUI();
}

/**
 * 폼이 렌더 직후 그대로면 원본 분 값, 사용자가 건드렸으면 입력값 × 단위.
 * (상태를 바꾸지 않는 조회 전용 — 힌트 계산과 실제 수집이 같은 값을 보게 한다.)
 */
function _resolveTargetMinutes() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return normalizeTargetDuration(sim.target_duration_minutes);
  const raw = String(valEl.value).trim();
  if (raw === _renderedDuration.value && unitEl.value === _renderedDuration.unit) {
    return _renderedDuration.minutes;
  }
  return durationPartsToMinutes(raw, unitEl.value);
}

/**
 * 폼 → target_duration_minutes.
 * 렌더 직후 그대로면 원본 분 값을 반환(근사 표시로 인한 값 유실 방지),
 * 사용자가 숫자나 단위를 바꿨으면 입력값 × 단위로 새로 환산한다.
 */
function _readTargetDuration() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  // 설정 패널이 아직 렌더되지 않은 경로에서는 기존 상태를 그대로 유지한다 (temperature와 같은 규칙).
  if (!valEl || !unitEl) return normalizeTargetDuration(sim.target_duration_minutes);

  const minutes = _resolveTargetMinutes();
  const raw     = String(valEl.value).trim();
  // 사용자가 직접 입력한 값이 되었으므로 이후로는 그 값이 정확한 기준이 된다.
  if (raw !== _renderedDuration.value || unitEl.value !== _renderedDuration.unit) {
    _renderedDuration = { value: raw, unit: unitEl.value, minutes, exact: true };
  }
  return minutes;
}

/** 현재 폼 상태 기준으로 목표 기간 입력의 활성/비활성과 안내 문구를 갱신. */
function _updateTargetDurationUI() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  const hintEl = document.getElementById('sim-target-duration-hint');
  if (!valEl || !unitEl || !hintEl) return;

  // 시간 모드/wave당 시간은 폼에서 실시간으로 읽는다 — 아직 sim에 반영되기 전일 수 있다.
  const mode = document.getElementById('sim-time-mode')?.value === 'variable' ? 'variable' : 'fixed';
  const tpw  = parseInt(document.getElementById('sim-time-per-wave')?.value) || 0;
  const off  = isTimeConceptDisabled(mode, tpw);

  valEl.disabled  = off;
  unitEl.disabled = off;
  hintEl.classList.remove('sim-hint-warn', 'sim-hint-off');

  if (off) {
    hintEl.classList.add('sim-hint-off');
    hintEl.textContent = '시간 개념이 꺼져 있어 사용할 수 없습니다 (시간 모드 "고정" + wave당 시간 0분).';
    return;
  }

  const minutes = _resolveTargetMinutes();
  if (minutes === null) {
    hintEl.textContent = '비워두면 목표 기간 없이 최대 wave 수까지 실행합니다.';
    return;
  }

  // 저장된 분 값이 어떤 단위로도 딱 떨어지지 않아 근사치로 표시 중이면 그 사실을 밝힌다
  // (실제로 저장/전송되는 값은 아래 "= N분"이며 입력을 건드리지 않는 한 그대로 유지된다).
  const approx = !_renderedDuration.exact &&
                 String(valEl.value).trim() === _renderedDuration.value &&
                 unitEl.value === _renderedDuration.unit;
  let text = `= ${minutes.toLocaleString('ko-KR')}분${approx ? '(표시는 근사치)' : ''}`
           + ' · 이번 실행에서 이만큼 진행되면 종료됩니다';
  // 고정 모드에서는 필요한 wave 수가 결정론적으로 계산된다 — 안전장치(max_waves)가
  // 먼저 걸리면 목표 기간에 닿지 못하므로 미리 알려준다.
  if (mode === 'fixed' && tpw > 0) {
    const needed   = Math.ceil(minutes / tpw);
    const maxWaves = parseInt(document.getElementById('sim-max-waves')?.value) || 10;
    text += ` · 약 ${needed.toLocaleString('ko-KR')} wave 필요`;
    if (needed > maxWaves) {
      hintEl.classList.add('sim-hint-warn');
      text += ` — 최대 wave 수(${maxWaves})에서 먼저 종료됩니다`;
    }
  }
  hintEl.textContent = text;
}

export function initTargetDurationUI() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return;

  const sync = () => {
    sim.target_duration_minutes = _readTargetDuration();
    _updateTargetDurationUI();
  };
  valEl.addEventListener('input',  sync);
  unitEl.addEventListener('change', sync);
  // 시간 모드/wave당 시간/최대 wave 수는 목표 기간의 활성 여부와 안내 문구에 영향을 준다.
  document.getElementById('sim-time-per-wave')?.addEventListener('input', _updateTargetDurationUI);
  document.getElementById('sim-max-waves')?.addEventListener('input', _updateTargetDurationUI);
}

// ── 가변 시간 모드 UI 연동 ────────────────────────────────────────────────────

function _updateVariableTimeUI(mode) {
  const section = document.getElementById('sim-variable-time-section');
  if (section) section.classList.toggle('sim-hidden', mode !== 'variable');
}

function _renderTimeCategories() {
  const cats = sim.time_categories?.length ? sim.time_categories : DEFAULT_TIME_CATEGORIES;
  document.querySelectorAll('.sim-time-cat-row').forEach(row => {
    const id = row.dataset.catId;
    const cat = cats.find(c => c.id === id) || DEFAULT_TIME_CATEGORIES.find(c => c.id === id);
    if (!cat) return;
    const labelEl = row.querySelector('.sim-time-cat-label');
    const minEl   = row.querySelector('.sim-time-cat-min');
    const maxEl   = row.querySelector('.sim-time-cat-max');
    if (labelEl) labelEl.value = cat.label;
    if (minEl)   minEl.value   = cat.min_minutes;
    if (maxEl)   maxEl.value   = cat.max_minutes;
  });
}

// 카테고리는 항상 4개 슬롯이 DOM에 고정되어 있으므로 빈 배열이 나올 일은 없지만,
// 방어적으로 비어 있으면 기본값으로 폴백한다 (백엔드가 [] 를 422로 거부함).
function _readTimeCategories() {
  const rows = document.querySelectorAll('.sim-time-cat-row');
  const result = [];
  rows.forEach(row => {
    const id = row.dataset.catId;
    if (!id) return;
    const fallback = DEFAULT_TIME_CATEGORIES.find(c => c.id === id) || {};
    const labelEl  = row.querySelector('.sim-time-cat-label');
    const minEl    = row.querySelector('.sim-time-cat-min');
    const maxEl    = row.querySelector('.sim-time-cat-max');
    const label    = labelEl?.value.trim() || fallback.label || id;
    let min = parseInt(minEl?.value);
    if (isNaN(min) || min < 1) min = fallback.min_minutes ?? 5;
    let max = parseInt(maxEl?.value);
    if (isNaN(max) || max < min) max = Math.max(min, fallback.max_minutes ?? min);
    result.push({ id, label, min_minutes: min, max_minutes: max });
  });
  return result.length ? result : DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
}

// idle_minutes_schedule: 콤마 구분 텍스트 → 정수 배열. 빈 배열이 되면 안 됨(백엔드 422 방지).
function _readIdleSchedule() {
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
    _updateVariableTimeUI(sim.time_mode);
    // 'fixed' + wave당 시간 0 조합에서만 목표 기간이 비활성이므로 모드 전환 시 함께 갱신한다.
    _updateTargetDurationUI();
  };
}

// ── system 에이전트 설정 렌더링 ───────────────────────────────────────────────

function renderSystemAgentConfig() {
  const sa      = sim.system_agent || {};
  const enabled = !!sa.enabled;
  const chk     = document.getElementById('sim-sys-enabled');
  const cfg     = document.getElementById('sim-sys-config');
  if (!chk || !cfg) return;

  chk.checked = enabled;
  cfg.classList.toggle('sim-hidden', !enabled);

  document.getElementById('sim-sys-icon').value          = sa.icon                  || '🎬';
  document.getElementById('sim-sys-display-name').value  = sa.display_name          || '내레이터';
  document.getElementById('sim-sys-prompt').value        = sa.system_prompt         || '';
  document.getElementById('sim-sys-interval').value      = sa.intervention_interval ?? 1;
  document.getElementById('sim-sys-silence').value       = sa.silence_threshold     ?? 3;
  document.getElementById('sim-sys-director-note').value = sa.director_note         || '';

  chk.onchange = () => {
    sim.system_agent.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
  };
}

// ── 감염병 모델 설정 ───────────────────────────────────────────────────────────
// 폼 ↔ sim.infection_model 동기화. 서버 검증(확률 0~1, max_waves >= min_waves)을
// 통과하도록 읽어들이는 모든 경로가 buildInfectionModel()을 거친다.

function renderInfectionConfig() {
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
  _bindProbabilitySlider('sim-inf-recovery',     'recovery_probability');
  _renderSymptomStages();

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

// 증상 단계 에디터 — time_categories 에디터와 같은 구조(id/label/min/max)에
// symptom_text textarea를 더한 형태. 단위는 분이 아니라 **웨이브**다.
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
        <input type="number" class="sim-inf-stage-min" data-idx="${idx}"
               min="0" max="999" value="${stage.min_waves}"/>
        <span class="sim-inf-stage-sep">~</span>
        <input type="number" class="sim-inf-stage-max" data-idx="${idx}"
               min="0" max="999" value="${stage.max_waves}"/>
        <span class="sim-inf-stage-unit">웨이브</span>
        <button class="sim-inf-stage-del" data-idx="${idx}" title="단계 삭제">×</button>
      </div>
      <textarea class="sim-inf-stage-text" data-idx="${idx}" rows="2"
                placeholder="이 단계에서 에이전트가 느끼는 몸 상태를 서술하세요. 이 문장이 LLM에게 전달되는 유일한 정보입니다.">${esc(stage.symptom_text)}</textarea>`;
    container.appendChild(row);
  });

  // 입력은 즉시 상태에 반영한다 — 삭제/추가로 다시 그릴 때 편집 중이던 값이 날아가지 않게.
  container.oninput = e => {
    const el  = e.target;
    const idx = parseInt(el.dataset?.idx);
    const stage = sim.infection_model.symptom_stages[idx];
    if (!stage) return;
    if (el.classList.contains('sim-inf-stage-label'))     stage.label        = el.value;
    else if (el.classList.contains('sim-inf-stage-text')) stage.symptom_text = el.value;
    else if (el.classList.contains('sim-inf-stage-min'))  stage.min_waves    = el.value;
    else if (el.classList.contains('sim-inf-stage-max'))  stage.max_waves    = el.value;
  };
  // min/max는 포커스를 벗어날 때(change) 정규화해서 다시 그린다 — 그래야 사용자가
  // 방금 고친 값이 실제로 clamp된 뒤의 값과 화면에서 어긋나지 않는다. 매 키 입력마다
  // (oninput) 다시 그리면 타이핑 중간에 커서가 날아가므로 change 시점에만 한다.
  container.onchange = e => {
    const el = e.target;
    if (!el.classList.contains('sim-inf-stage-min') && !el.classList.contains('sim-inf-stage-max')) return;
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

/** "+ 단계 추가" 버튼 — 마지막 단계 다음 웨이브부터 시작하는 빈 단계를 붙인다. */
export function addSymptomStage() {
  sim.infection_model = buildInfectionModel(sim.infection_model);
  const stages = sim.infection_model.symptom_stages;
  const last   = stages[stages.length - 1];
  const start  = last ? last.max_waves + 1 : 0;
  stages.push({
    id:           `stage${stages.length + 1}`,
    label:        `단계 ${stages.length + 1}`,
    min_waves:    start,
    max_waves:    start + 2,
    symptom_text: '',
  });
  _renderSymptomStages();
}

/** 폼 → infection_model. 설정 패널이 렌더되지 않은 경로에서는 기존 상태를 유지한다. */
function _readInfectionModel() {
  const chk = document.getElementById('sim-inf-enabled');
  if (!chk) return buildInfectionModel(sim.infection_model);
  return buildInfectionModel({
    enabled:                  chk.checked,
    disease_name:             document.getElementById('sim-inf-disease-name')?.value ?? '',
    transmission_probability: document.getElementById('sim-inf-transmission')?.value,
    recovery_probability:     document.getElementById('sim-inf-recovery')?.value,
    // 편집기가 비어 있으면 빈 배열 그대로 — 백엔드도 빈 목록을 허용한다(증상 없음).
    symptom_stages:           sim.infection_model?.symptom_stages ?? [],
    immune_after_recovery:    document.getElementById('sim-inf-immune')?.value !== 'sis',
  });
}

// ── 샘플링 온도 슬라이더 ───────────────────────────────────────────────────────

// 시뮬레이션 전체 기본 temperature. 에이전트 카드의 온도 입력은 이 값을 상속하므로,
// 슬라이더를 움직이면 카드들의 placeholder("기본값 0.7")도 같이 갱신한다.
function renderTemperatureSlider() {
  const slider = document.getElementById('sim-temperature');
  const valEl  = document.getElementById('sim-temperature-val');
  if (!slider) return;

  const temp = normalizeTemperature(sim.temperature);
  sim.temperature = temp;              // 구버전 시나리오의 범위 밖 값을 상태에서도 바로잡는다
  slider.value = String(temp);
  if (valEl) valEl.textContent = temp.toFixed(1);

  // renderSettingsPage()는 여러 번 호출되므로 addEventListener 대신 oninput으로
  // 덮어써서 리스너가 중복 등록되지 않게 한다 (renderServerSelect의 onchange와 동일한 규칙).
  slider.oninput = () => {
    sim.temperature = normalizeTemperature(slider.value);
    if (valEl) valEl.textContent = sim.temperature.toFixed(1);
    refreshAgentTempPlaceholders();
  };
}

// ── 서버 드롭다운 ─────────────────────────────────────────────────────────────

async function renderServerSelect() {
  const sel  = document.getElementById('sim-server-select');
  const hint = document.getElementById('sim-server-hint');
  if (!sel) return;

  const servers = await getServerList();

  sel.innerHTML = `<option value="">기본 서버</option>` +
    servers.map(s =>
      `<option value="${s.id}">${s.name}</option>`
    ).join('');

  // 저장된 server_id로 선택값 복원
  sel.value = sim.server_id || '';

  updateServerHint(sel.value, servers);

  sel.onchange = () => {
    sim.server_id = sel.value || null;
    updateServerHint(sel.value, servers);
  };
}

function updateServerHint(serverId, servers) {
  const hint = document.getElementById('sim-server-hint');
  if (!hint) return;
  if (!serverId) {
    const def = servers.find(s => s.is_default);
    hint.textContent = def
      ? `기본: ${def.model.split('/').pop()} · ${def.base_url}`
      : '등록된 서버가 없으면 환경변수 설정을 사용합니다';
  } else {
    const s = servers.find(s => s.id === serverId);
    hint.textContent = s ? `${s.model.split('/').pop()} · ${s.base_url}` : '';
  }
}

// ── 위치 그래프 에디터 ─────────────────────────────────────────────────────────

function renderLocationGraph() {
  const container = document.getElementById('sim-location-graph');
  if (!container) return;
  const graph = sim.location_graph || [];
  const nodeNames = graph.map(n => n.name);
  container.innerHTML = '';

  graph.forEach((node, idx) => {
    const row = document.createElement('div');
    const isExt = !!node.is_exterior;
    row.className = `sim-loc-node-row${isExt ? ' sim-loc-exterior' : ''}`;
    const connBadges = (node.connects_to || []).map(c => `
      <span class="sim-loc-conn-badge">
        ${esc(c)}
        <button class="sim-loc-conn-del" data-node="${idx}" data-conn="${esc(c)}" title="연결 제거">×</button>
      </span>`).join('');
    const otherNodes = nodeNames.filter(n => n !== node.name && !(node.connects_to || []).includes(n));
    const addConnOpts = otherNodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    row.innerHTML = `
      <div class="sim-loc-node-header">
        <span class="sim-loc-node-icon">${isExt ? '🌐' : '📍'}</span>
        <input class="sim-loc-node-name" data-idx="${idx}" value="${esc(node.name)}" placeholder="장소 이름" />
        <label class="sim-loc-exterior-toggle" title="외부 공간 — 이 장소에 있는 에이전트는 서로를 볼 수 없고 내부와 소통 불가">
          <input type="checkbox" class="sim-loc-exterior-chk" data-idx="${idx}" ${isExt ? 'checked' : ''}>
          <span>외부</span>
        </label>
        <button class="sim-loc-node-del" data-idx="${idx}" title="장소 삭제">×</button>
      </div>
      <div class="sim-loc-conns">
        <span class="sim-loc-conns-label">연결:</span>
        ${connBadges || '<span class="sim-loc-no-conn">(없음)</span>'}
        ${addConnOpts ? `<select class="sim-loc-add-conn" data-node="${idx}">
          <option value="">+ 연결 추가</option>
          ${addConnOpts}
        </select>` : ''}
      </div>`;
    container.appendChild(row);
  });

  // 이벤트 위임
  container.onclick = e => {
    const delNode = e.target.closest('.sim-loc-node-del');
    if (delNode) {
      const i = parseInt(delNode.dataset.idx);
      const name = sim.location_graph[i].name;
      sim.location_graph.splice(i, 1);
      sim.location_graph.forEach(n => {
        n.connects_to = (n.connects_to || []).filter(c => c !== name);
      });
      renderLocationGraph();
      return;
    }
    const delConn = e.target.closest('.sim-loc-conn-del');
    if (delConn) {
      const ni = parseInt(delConn.dataset.node);
      const conn = delConn.dataset.conn;
      sim.location_graph[ni].connects_to = (sim.location_graph[ni].connects_to || []).filter(c => c !== conn);
      const other = sim.location_graph.find(n => n.name === conn);
      if (other) other.connects_to = (other.connects_to || []).filter(c => c !== sim.location_graph[ni].name);
      renderLocationGraph();
      return;
    }
  };

  container.onchange = e => {
    const extChk = e.target.closest('.sim-loc-exterior-chk');
    if (extChk) {
      const i = parseInt(extChk.dataset.idx);
      sim.location_graph[i].is_exterior = extChk.checked;
      renderLocationGraph();
      return;
    }
    const nameInput = e.target.closest('.sim-loc-node-name');
    if (nameInput) {
      const i = parseInt(nameInput.dataset.idx);
      const oldName = sim.location_graph[i].name;
      const newName = nameInput.value.trim();
      if (newName && newName !== oldName) {
        sim.location_graph.forEach(n => {
          n.connects_to = (n.connects_to || []).map(c => c === oldName ? newName : c);
        });
        sim.location_graph[i].name = newName;
        renderLocationGraph();
      }
      return;
    }
    const addConn = e.target.closest('.sim-loc-add-conn');
    if (addConn) {
      const ni = parseInt(addConn.dataset.node);
      const target = addConn.value;
      if (!target) return;
      const nodeA = sim.location_graph[ni];
      const nodeB = sim.location_graph.find(n => n.name === target);
      if (!nodeA.connects_to) nodeA.connects_to = [];
      if (!nodeA.connects_to.includes(target)) nodeA.connects_to.push(target);
      if (nodeB) {
        if (!nodeB.connects_to) nodeB.connects_to = [];
        if (!nodeB.connects_to.includes(nodeA.name)) nodeB.connects_to.push(nodeA.name);
      }
      renderLocationGraph();
      return;
    }
  };
}

function _readLocationGraph() {
  return (sim.location_graph || []).map(n => ({
    name:        n.name,
    connects_to: [...(n.connects_to || [])],
    is_exterior: !!n.is_exterior,
  }));
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [] });
  renderLocationGraph();
}
