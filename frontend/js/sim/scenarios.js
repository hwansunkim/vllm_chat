// frontend/js/sim/scenarios.js
// Scenario list CRUD + current-scenario selection.

import { sim, _expandedAgents, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, DEFAULT_START_WEEKDAY, normalizeWeekday,
         DEFAULT_TEMPERATURE, normalizeTemperature, normalizeAgentTemperature,
         normalizeTargetDuration, buildInfectionModel } from './state.js';
import { renderSettingsPage, readConfigFromUI } from './settings/page.js';
import { refreshRunHistory } from './runs/history.js';
import { downloadFile, safeFilename, nowTag } from './utils/download.js';

// 파일로 내보낸 시나리오를 다시 불러올 때 쓰는 스키마 버전.
// 필드가 추가되고 기본값(??)으로 충분히 처리되는 변경에는 올리지 않는다 — 그건 이미
// applyScenario()의 낙관적 로딩이 처리한다. 필드의 의미/이름이 바뀌어 자동으로는 채울 수
// 없는 "진짜 breaking change"가 생겼을 때만 올리고, 아래 MIGRATIONS에 변환 함수를 추가한다.
const CURRENT_SCHEMA_VERSION = 1;

// 키: 마이그레이션 대상 버전(그 버전에서 다음 버전으로 올리는 변환). 값: config => config.
// 지금은 필요한 마이그레이션이 없으므로 빈 상태로 시작 — 확장점만 마련해둔다.
const MIGRATIONS = {};

const APP_ID = 'vllm-chat-sim-scenario';

/**
 * 저장된 시나리오 목록을 그대로 가져온다 (각 항목에 전체 `config` 포함).
 * DOM 도, sim 상태도 건드리지 않으므로 시뮬레이션 화면 밖(예: 채팅 에이전트 관리의
 * "시뮬레이션에서 가져오기")에서도 안전하게 재사용할 수 있다.
 */
export async function fetchScenarioList() {
  const res = await fetch('/api/simulation/scenarios');
  if (!res.ok) throw new Error(`시나리오 목록을 불러오지 못했습니다. (HTTP ${res.status})`);
  const list = await res.json();
  return Array.isArray(list) ? list : [];
}

export async function loadScenarios() {
  sim.scenarios = await fetchScenarioList();
  const sel = document.getElementById('sim-scenario-select');
  sel.innerHTML = '<option value="">-- 불러오기 --</option>';
  sim.scenarios.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    sel.appendChild(opt);
  });
  // 현재 시나리오가 로드된 상태면 이력 버튼 활성화
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) histBtn.disabled = !sim.currentScenarioId;
}

export function buildScenarioConfig() {
  return {
    agents:                 sim.agents,
    background:             sim.background,
    start_agent:            sim.start_agent,
    max_waves:              sim.max_waves,
    // 목표 기간(분). null = 미사용 — 백엔드가 0/음수를 422로 거부하므로 정규화해서 저장한다.
    target_duration_minutes: normalizeTargetDuration(sim.target_duration_minutes),
    step_delay:             sim.step_delay,
    token_limit:            sim.token_limit,
    llm_max_tokens:         sim.llm_max_tokens,
    extra_fields:           sim.extra_fields,
    events:                 sim.events,
    location_graph:         sim.location_graph || [],
    lang_fix_enabled:       sim.lang_fix_enabled ?? true,
    lang_fix_retries:       sim.lang_fix_retries ?? 2,
    output_format_template: sim.output_format_template || '',
    summary_interval:       sim.summary_interval  ?? 0,
    sim_start_time:         sim.sim_start_time    ?? '09:00',
    sim_start_weekday:      normalizeWeekday(sim.sim_start_weekday),
    time_per_wave:          sim.time_per_wave     ?? 30,
    time_mode:              sim.time_mode ?? 'fixed',
    time_categories:        (sim.time_categories?.length ? sim.time_categories : DEFAULT_TIME_CATEGORIES),
    idle_minutes_schedule:  (sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE),
    max_silence_waves:      sim.max_silence_waves  ?? 3,
    early_stop_enabled:     sim.early_stop_enabled ?? true,
    server_id:              sim.server_id         ?? null,
    temperature:            normalizeTemperature(sim.temperature),
    system_agent:           sim.system_agent,
    // 확률 범위(0~1)와 max_waves >= min_waves를 서버가 422로 거부하므로 정규화해서 저장한다.
    infection_model:        buildInfectionModel(sim.infection_model),
  };
}

export async function saveScenario() {
  readConfigFromUI();
  const nameEl = document.getElementById('sim-scenario-name');
  const name   = nameEl.value.trim();
  if (!name) { nameEl.focus(); nameEl.style.borderColor = '#ef4444'; return; }
  nameEl.style.borderColor = '';

  const payload = {
    name,
    description: '',
    config: buildScenarioConfig(),
  };

  let res;
  if (sim.currentScenarioId) {
    res = await fetch(`/api/simulation/scenarios/${sim.currentScenarioId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } else {
    res = await fetch('/api/simulation/scenarios', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      const data = await res.json();
      sim.currentScenarioId = data.id;
    }
  }

  if (res.ok) {
    sim.currentScenarioName = name;
    const delBtn = document.getElementById('sim-delete-scenario-btn');
    if (delBtn) delBtn.disabled = false;
    await loadScenarios();
    const btn = document.getElementById('sim-save-scenario-btn');
    const orig = btn.textContent;
    btn.textContent = '✓ 저장됨';
    btn.style.color = '#15803d';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
  }
}

export async function deleteScenario() {
  if (!sim.currentScenarioId) return;
  if (!confirm(`시나리오 "${sim.currentScenarioName}"을(를) 삭제하시겠습니까?`)) return;
  await fetch(`/api/simulation/scenarios/${sim.currentScenarioId}`, { method: 'DELETE' });
  sim.currentScenarioId   = null;
  sim.currentScenarioName = '';
  document.getElementById('sim-scenario-name').value = '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = true;
  await loadScenarios();
}

export function newScenario() {
  sim.currentScenarioId   = null;
  sim.currentScenarioName = '';
  sim.agents              = [];
  sim.background          = '';
  sim.start_agent         = '';
  sim.max_waves           = 10;
  sim.target_duration_minutes = null;
  sim.step_delay          = 1.0;
  sim.token_limit         = 8192;
  sim.llm_max_tokens      = 16384;
  sim.extra_fields        = [
    { name: 'emotion',     default: 'neutral' },
    { name: 'action',      default: 'speak'   },
    { name: 'action_note', default: ''        },
  ];
  sim.events                 = [];
  sim.location_graph         = [];
  sim.lang_fix_enabled       = true;
  sim.lang_fix_retries       = 2;
  sim.output_format_template = '';
  sim.summary_interval       = 0;
  sim.sim_start_time         = '09:00';
  sim.sim_start_weekday      = DEFAULT_START_WEEKDAY;
  sim.time_per_wave          = 30;
  sim.time_mode              = 'fixed';
  sim.time_categories        = DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
  sim.idle_minutes_schedule  = [...DEFAULT_IDLE_MINUTES_SCHEDULE];
  sim.max_silence_waves      = 3;
  sim.early_stop_enabled     = true;
  sim.server_id              = null;
  sim.temperature            = DEFAULT_TEMPERATURE;
  sim.system_agent           = { enabled: false, icon: '🎬', display_name: '내레이터', system_prompt: '', intervention_interval: 1, silence_threshold: 3, director_note: '' };
  // 꺼진 상태 + 바로 쓸 수 있는 기본 증상 단계 (buildInfectionModel의 기본값).
  sim.infection_model        = buildInfectionModel(null);
  _expandedAgents.clear();
  document.getElementById('sim-scenario-name').value = '';
  document.getElementById('sim-scenario-select').value = '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = true;
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) { histBtn.disabled = true; histBtn.textContent = '📋 이력'; }
  document.getElementById('sim-run-history-panel')?.classList.add('sim-hidden');
  renderSettingsPage();
}

export function applyScenario(s) {
  const cfg = s.config;
  sim.currentScenarioId   = s.id;
  sim.currentScenarioName = s.name;
  sim.agents       = (cfg.agents || []).map(a => ({
    ...a,
    groups:             a.groups             || [],
    location:           a.location           ?? '',
    visual_description: a.visual_description ?? '',
    // 구버전 시나리오에는 필드 자체가 없다 — null(= 시뮬레이션 기본 서버)로 채운다.
    server_id:          a.server_id          ?? null,
    // 마찬가지로 없으면 null(= 시뮬레이션 기본 temperature 사용).
    temperature:        normalizeAgentTemperature(a.temperature),
  }));
  sim.background   = cfg.background   || '';
  sim.start_agent  = cfg.start_agent  || (cfg.agents?.[0]?.name ?? '');
  sim.max_waves    = cfg.max_waves    || 10;
  // 구버전 시나리오에는 필드가 없다 — normalizeTargetDuration()이 null(= 미사용)로 폴백한다.
  sim.target_duration_minutes = normalizeTargetDuration(cfg.target_duration_minutes);
  sim.step_delay   = cfg.step_delay   || 1.0;
  // Migrate legacy memory_limit (message count) to token_limit.
  // Old default was 20 messages; ~400 tokens/message is a reasonable estimate.
  sim.token_limit     = cfg.token_limit ?? (cfg.memory_limit ? cfg.memory_limit * 400 : 8192);
  sim.llm_max_tokens  = cfg.llm_max_tokens ?? 16384;
  sim.extra_fields = cfg.extra_fields || [
    { name: 'emotion',     default: 'neutral' },
    { name: 'action',      default: 'speak'   },
    { name: 'action_note', default: ''        },
  ];
  // Back-compat: ensure action_note exists even in scenarios saved before it was added.
  if (!sim.extra_fields.some(f => f.name === 'action_note')) {
    sim.extra_fields.push({ name: 'action_note', default: '' });
  }
  sim.events                 = cfg.events                 || [];
  // zone은 구버전 시나리오에 없다 — undefined가 남지 않도록 ''로 정규화한다.
  sim.location_graph = (cfg.location_graph || []).map(n => ({ ...n, connects_to: [...(n.connects_to || [])], is_exterior: !!n.is_exterior, zone: (n.zone || '').trim() }));
  sim.lang_fix_enabled       = cfg.lang_fix_enabled       ?? true;
  sim.lang_fix_retries       = cfg.lang_fix_retries       ?? 2;
  sim.output_format_template = cfg.output_format_template || '';
  sim.summary_interval       = cfg.summary_interval       ?? 0;
  sim.sim_start_time         = cfg.sim_start_time         ?? '09:00';
  // 구버전 시나리오에는 필드가 없다 — normalizeWeekday()가 'mon'으로 폴백한다.
  sim.sim_start_weekday      = normalizeWeekday(cfg.sim_start_weekday);
  sim.time_per_wave          = cfg.time_per_wave          ?? 30;
  sim.time_mode              = cfg.time_mode === 'variable' ? 'variable' : 'fixed';
  sim.time_categories        = cfg.time_categories?.length ? cfg.time_categories : DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
  sim.idle_minutes_schedule  = cfg.idle_minutes_schedule?.length ? cfg.idle_minutes_schedule : [...DEFAULT_IDLE_MINUTES_SCHEDULE];
  sim.max_silence_waves      = cfg.max_silence_waves      ?? 3;
  sim.early_stop_enabled     = cfg.early_stop_enabled     ?? true;
  sim.server_id              = cfg.server_id              ?? null;
  // 구버전 시나리오에는 필드가 없다 — normalizeTemperature()가 0.7로 폴백한다.
  sim.temperature            = normalizeTemperature(cfg.temperature);
  const sa = cfg.system_agent ?? {};
  sim.system_agent = {
    enabled:               sa.enabled               ?? false,
    icon:                  sa.icon                  ?? '🎬',
    display_name:          sa.display_name          ?? '내레이터',
    system_prompt:         sa.system_prompt         ?? '',
    intervention_interval: sa.intervention_interval ?? 1,
    silence_threshold:     sa.silence_threshold     ?? 3,
    director_note:         sa.director_note         ?? '',
  };
  // 구버전 시나리오에는 필드가 없다 — buildInfectionModel()이 "꺼진 모델 + 기본 증상 단계"로 폴백한다.
  sim.infection_model        = buildInfectionModel(cfg.infection_model);
  _expandedAgents.clear();
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) {
    histBtn.disabled = false;
    histBtn.textContent = '📋 이력';
  }
  // 이력 패널이 열려있으면 갱신
  refreshRunHistory();
  renderSettingsPage();
}

// ── File export/import (백엔드 호출 없음, DB에는 저장하지 않음) ──────────────────

export function exportScenarioFile() {
  readConfigFromUI();
  const config = buildScenarioConfig();
  const envelope = {
    app: APP_ID,
    schema_version: CURRENT_SCHEMA_VERSION,
    exported_at: new Date().toISOString(),
    name: sim.currentScenarioName || '시나리오',
    description: '',
    config,
  };
  const filename = `${safeFilename(sim.currentScenarioName || '시나리오')}_${nowTag()}.json`;
  downloadFile(JSON.stringify(envelope, null, 2), filename, 'application/json;charset=utf-8');
}

export async function importScenarioFile(file) {
  let parsed;
  try {
    const text = await file.text();
    parsed = JSON.parse(text);
  } catch (e) {
    alert('올바른 JSON 파일이 아닙니다.');
    return;
  }

  let config;
  if (parsed && typeof parsed === 'object' && parsed.config && typeof parsed.config === 'object') {
    config = parsed.config;
  } else if (parsed && Array.isArray(parsed.agents)) {
    // envelope 없는 날것의 config 파일 허용 (구버전/외부 생성 파일).
    config = parsed;
  } else {
    alert('올바른 시나리오 파일이 아닙니다.');
    return;
  }

  let version = typeof parsed.schema_version === 'number' ? parsed.schema_version : 0;
  if (version > CURRENT_SCHEMA_VERSION) {
    alert('이 파일은 더 최신 버전에서 내보내졌습니다. 일부 설정이 무시됐을 수 있습니다.');
  } else {
    while (version < CURRENT_SCHEMA_VERSION) {
      const migrate = MIGRATIONS[version];
      if (migrate) config = migrate(config);
      version += 1;
    }
  }

  sim.currentScenarioId = null;
  applyScenario({ id: null, name: parsed.name || '가져온 시나리오', config });
  document.getElementById('sim-scenario-name').value = sim.currentScenarioName;
}
