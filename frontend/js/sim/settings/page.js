// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).
//
// 개별 설정 영역의 렌더/수집 로직은 기능별 모듈에 있다. 여기서는 그 모듈들을
// 올바른 순서로 호출하는 일과, 기존 사용처를 위해 공개 API를 재수출하는 일만 한다.

import { sim, normalizeWeekday, normalizeTemperature,
         DEFAULT_IDLE_MINUTES_SCHEDULE } from '../state.js';
import { renderOutputFields } from './output-fields.js';
import { renderAgentListInConfig, renderStartAgentSelect } from './agents.js';
import { renderScenarioEvents } from './events.js';
import { invalidateServerList } from './server-list.js';
import { applySectionState, updateSectionBadges } from './sections.js';
import { autoGrowAll } from './textareas.js';
import { updateEarlyStopUI, initEarlyStopToggle } from './early-stop.js';
import { renderTargetDuration, readTargetDuration, initTargetDurationUI } from './target-duration.js';
import { updateVariableTimeUI, renderTimeCategories, readTimeCategories,
         readIdleSchedule, addTimeCategory, initTimeModeToggle,
         normalizeTimeEstimationMode, updateTimeEstimationModeUI,
         readTimeEstimationMode, initTimeEstimationModeToggle } from './time-categories.js';
import { renderSystemAgentConfig } from './system-agent.js';
import { renderInfectionConfig, readInfectionModel, addSymptomStage } from './infection-config.js';
import { renderTemperatureSlider } from './temperature.js';
import { renderServerSelect } from './server-select.js';
import { renderLocationGraph, readLocationGraph, addLocationNode,
         renderPerceptionMode, readPerceptionMode, initPerceptionModeToggle } from './location-graph.js';
import { renderContractPreview, readOutputFormatOverride } from './contract-preview.js';

// 기존 사용처(index.js 등)가 계속 './settings/page.js' 하나만 import 하도록 재수출한다.
export { initEarlyStopToggle, initTargetDurationUI, initTimeModeToggle,
         initTimeEstimationModeToggle, initPerceptionModeToggle,
         addTimeCategory, addSymptomStage, addLocationNode };

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
  const startTimeEl = document.getElementById('sim-start-time');
  if (startTimeEl) startTimeEl.value = sim.sim_start_time ?? '09:00';
  const startWeekdayEl = document.getElementById('sim-start-weekday');
  if (startWeekdayEl) startWeekdayEl.value = normalizeWeekday(sim.sim_start_weekday);
  const timePerWaveEl = document.getElementById('sim-time-per-wave');
  if (timePerWaveEl) timePerWaveEl.value = sim.time_per_wave ?? 30;
  const timeModeEl = document.getElementById('sim-time-mode');
  if (timeModeEl) {
    timeModeEl.value = sim.time_mode === 'variable' ? 'variable' : 'fixed';
    updateVariableTimeUI(timeModeEl.value);
  }
  renderTargetDuration();
  const timeEstEl = document.getElementById('sim-time-estimation-mode');
  if (timeEstEl) {
    timeEstEl.value = normalizeTimeEstimationMode(sim.time_estimation_mode);
    updateTimeEstimationModeUI(timeEstEl.value);
  }
  renderTimeCategories();
  const idleScheduleEl = document.getElementById('sim-idle-schedule');
  if (idleScheduleEl) {
    const sched = sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE;
    idleScheduleEl.value = sched.join(',');
  }
  const maxSceneJumpEl = document.getElementById('sim-max-scene-jump');
  if (maxSceneJumpEl) maxSceneJumpEl.value = sim.max_scene_jump_minutes ?? 45;
  const maxDaytimeJumpEl = document.getElementById('sim-max-daytime-jump');
  if (maxDaytimeJumpEl) maxDaytimeJumpEl.value = sim.max_daytime_jump_minutes ?? 180;
  const maxSilenceEl = document.getElementById('sim-max-silence-waves');
  if (maxSilenceEl) maxSilenceEl.value = sim.max_silence_waves ?? 3;
  const earlyStopEl = document.getElementById('sim-early-stop-enabled');
  if (earlyStopEl) {
    earlyStopEl.checked = sim.early_stop_enabled ?? true;
    updateEarlyStopUI(earlyStopEl.checked);
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
  // 감염 설정을 이벤트 편집기보다 먼저 그린다 — 환자 0번 피커가 삭제된 에이전트를
  // 가리키는 infect_agent 이벤트를 prune 하는데, 순서가 반대면 renderScenarioEvents()의
  // _syncAgentSelection()이 그 stale ref를 먼저 sim.agents[0]으로 옮겨버린다.
  renderInfectionConfig();     // 감염병 모델 설정 (+ 환자 0번 피커)
  renderScenarioEvents();
  renderPerceptionMode();      // 위치 그래프 섹션 상단의 공간 기반 인지 토글
  renderLocationGraph();
  renderServerSelect();        // 비동기 — 드롭다운 별도 렌더링
  renderSystemAgentConfig();   // system 에이전트 설정 동기 렌더링
  // 계약 미리보기는 위치 그래프·감염·시간 설정이 모두 DOM에 반영된 뒤여야 한다
  // (요청 페이로드를 그 입력들에서 직접 읽는다).
  renderContractPreview();

  // 레이아웃 마무리 — 내용이 모두 채워진 뒤여야 뱃지/높이가 맞는다.
  // (섹션이 접혀 있어도 readConfigFromUI()가 읽는 input/textarea는 DOM에 그대로 있다.
  //  접기는 display:none 이 아니라 grid-template-rows 클리핑이기 때문.)
  applySectionState(sim);
  updateSectionBadges(sim);
  autoGrowAll(document.getElementById('sim-settings-main'));
}

export function readConfigFromUI() {
  sim.background             = document.getElementById('sim-background').value.trim();
  sim.start_agent            = document.getElementById('sim-start-agent').value;
  sim.max_waves              = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.target_duration_minutes = readTargetDuration();
  sim.step_delay             = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)     || 8192;
  sim.llm_max_tokens         = parseInt(document.getElementById('sim-llm-max-tokens').value) || 16384;
  // 출력 계약 오버라이드 — 체크박스가 꺼져 있으면 언제나 ''(= 엔진 생성분 사용).
  sim.output_format_override = readOutputFormatOverride();
  sim.sim_start_time    = document.getElementById('sim-start-time')?.value || '09:00';
  sim.sim_start_weekday = normalizeWeekday(document.getElementById('sim-start-weekday')?.value);
  sim.time_per_wave     = parseInt(document.getElementById('sim-time-per-wave')?.value)     || 0;
  sim.time_mode         = document.getElementById('sim-time-mode')?.value === 'variable' ? 'variable' : 'fixed';
  sim.time_categories   = readTimeCategories();
  // time_mode='fixed'여도 값은 그대로 보존한다 — 엔진이 fixed에서 무시하므로 무해하고,
  // 모드를 오갈 때 사용자가 고른 추론 방식이 초기화되지 않는다.
  sim.time_estimation_mode  = readTimeEstimationMode();
  sim.idle_minutes_schedule = readIdleSchedule();
  // 점프 상한은 0이 유효값("캡 끔")이므로 `|| 기본값` 을 쓰면 안 된다 — 빈칸일 때만 기본값.
  const _sceneJump = document.getElementById('sim-max-scene-jump')?.value;
  sim.max_scene_jump_minutes = (_sceneJump == null || _sceneJump === '')
    ? 45 : Math.max(0, parseInt(_sceneJump) || 0);
  const _daytimeJump = document.getElementById('sim-max-daytime-jump')?.value;
  sim.max_daytime_jump_minutes = (_daytimeJump == null || _daytimeJump === '')
    ? 180 : Math.max(0, parseInt(_daytimeJump) || 0);
  sim.max_silence_waves  = parseInt(document.getElementById('sim-max-silence-waves')?.value)  || 3;
  sim.early_stop_enabled = document.getElementById('sim-early-stop-enabled')?.checked ?? true;
  const sel = document.getElementById('sim-server-select');
  sim.server_id              = sel?.value || null;
  // 슬라이더가 DOM에 있으면 그 값이 항상 우선이고(정규화는 범위 밖 대비),
  // 패널이 렌더되지 않은 상태로 호출되는 경로를 대비해 없을 때만 기존 상태로 폴백한다.
  const tempEl = document.getElementById('sim-temperature');
  sim.temperature = normalizeTemperature(tempEl ? tempEl.value : sim.temperature);
  sim.location_graph   = readLocationGraph();
  // 위치 그래프가 비어 있어도 값은 그대로 보존한다 — 엔진이 위치 없는 시나리오에서는
  // "전원이 같은 방"으로 해석하므로 무해하고, 장소를 지웠다 다시 넣을 때 초기화되지 않는다.
  sim.perception_mode  = readPerceptionMode();
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
    digest_waves:          parseInt(document.getElementById('sim-sys-digest')?.value)    || 6,
    director_note:         document.getElementById('sim-sys-director-note')?.value       || '',
  };
  sim.infection_model = readInfectionModel();
}
