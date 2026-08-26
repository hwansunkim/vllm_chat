// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).

import { sim, esc, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday,
         normalizeTemperature, DEFAULT_DURATION_UNIT, normalizeTargetDuration,
         minutesToDurationParts, durationPartsToMinutes, isTimeConceptDisabled } from '../state.js';
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

// 카테고리 개수는 자유다 — 백엔드(TimeCategory/_non_empty_categories)는 "비어 있지만 않으면"
// 개수 제한이 없다. 행은 전부 sim.time_categories 배열을 기준으로 동적으로 렌더링한다.
function _renderTimeCategories() {
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
    sim.time_categories = _readTimeCategories();
    const i = parseInt(del.dataset.idx);
    if (sim.time_categories.length <= 1) return;
    sim.time_categories.splice(i, 1);
    _renderTimeCategories();
  };
}

// 렌더된 행 수만큼 읽는다. 값이 비었거나 범위를 벗어나면 안전한 기본값으로 보정한다
// (백엔드는 min_minutes >= 1, max_minutes >= min_minutes를 요구함).
function _readTimeCategories() {
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
  sim.time_categories = _readTimeCategories();
  sim.time_categories.push({
    id: _newTimeCatId(sim.time_categories.map(c => c.id)),
    label: `카테고리 ${sim.time_categories.length + 1}`,
    min_minutes: 15,
    max_minutes: 30,
  });
  _renderTimeCategories();
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

  // zone 자유 텍스트의 오타로 "우리집"/"우리 집"이 서로 다른 zone으로 갈라지면 인지 기능이
  // 조용히 깨진다. 이미 쓰인 zone을 datalist로 제안해 표기를 통일시킨다.
  const zoneList = document.createElement('datalist');
  zoneList.id = 'sim-loc-zone-list';
  container.appendChild(zoneList);

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
        <input class="sim-loc-node-zone" data-idx="${idx}" list="sim-loc-zone-list"
               value="${esc(node.zone || '')}" placeholder="zone(선택)"
               title="구역 — 같은 zone의 장소에 있는 에이전트끼리는 서로의 존재를 인지합니다(대화는 같은 장소에서만 가능). 비워두면 zone 없음." />
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

  syncZoneDatalist();

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
    const zoneInput = e.target.closest('.sim-loc-node-zone');
    if (zoneInput) {
      const i = parseInt(zoneInput.dataset.idx);
      const zone = zoneInput.value.trim();
      zoneInput.value = zone;
      sim.location_graph[i].zone = zone;
      // 재렌더 없이 datalist만 갱신한다 — change는 blur 시점에 발생하므로 여기서
      // renderLocationGraph()를 부르면 다음 요소로 넘어가던 탭 포커스가 사라진다.
      syncZoneDatalist();
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

// 현재 그래프에 쓰인 zone 값들을 zone 입력의 자동완성 목록으로 채운다.
function syncZoneDatalist() {
  const dl = document.getElementById('sim-loc-zone-list');
  if (!dl) return;
  const zones = [...new Set(
    (sim.location_graph || []).map(n => (n.zone || '').trim()).filter(Boolean)
  )].sort();
  dl.innerHTML = '';
  zones.forEach(z => {
    const opt = document.createElement('option');
    opt.value = z;   // 속성 대입이라 이스케이프 불필요
    dl.appendChild(opt);
  });
}

function _readLocationGraph() {
  return (sim.location_graph || []).map((n, idx) => {
    // zone 입력은 change(=blur) 시점에 모델로 반영된다. 포커스가 남아 있는 채로
    // 설정을 떠나는 경로를 대비해 DOM 값이 있으면 그쪽을 우선한다. 패널이 렌더되지
    // 않은 상태로 불릴 수도 있으므로 없을 때만 기존 상태로 폴백한다.
    const zoneEl = document.querySelector(`#sim-location-graph .sim-loc-node-zone[data-idx="${idx}"]`);
    return {
      name:        n.name,
      connects_to: [...(n.connects_to || [])],
      is_exterior: !!n.is_exterior,
      zone:        (zoneEl ? zoneEl.value : (n.zone || '')).trim(),
    };
  });
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [], zone: '' });
  renderLocationGraph();
}
