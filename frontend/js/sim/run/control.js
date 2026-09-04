// frontend/js/sim/run/control.js
// Start / stop / continue / status-badge logic for simulation runs.

import { sim, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday,
         normalizeTemperature, normalizeTargetDuration, buildInfectionModel } from '../state.js';
import { readConfigFromUI } from '../settings/page.js';
import { renderAgentCards } from './cards.js';
import { removeTypingIndicator, resetWaveCardBuffer, flushPendingWaveCards } from './feed.js';
import { initD3Graph } from '../graph/d3.js';
import { initLocationMap } from '../map/d3.js';
import { connectSSE, disconnectSSE } from './sse.js';
import { clearErrorLog, renderErrorIndicator } from './errors.js';

export function setStatus(status) {
  sim.status = status;
  const badge  = document.getElementById('sim-status-badge');
  const labels = { idle: '대기 중', running: '실행 중', done: '완료', stopped: '중지됨', error: '오류' };
  badge.textContent = labels[status] || status;
  badge.className   = `sim-status-badge ${status}`;
  document.getElementById('sim-start-btn').disabled    = status === 'running';
  document.getElementById('sim-continue-btn').disabled = !['done', 'stopped'].includes(status);
  document.getElementById('sim-stop-btn').disabled     = status !== 'running';
  // MD 내보내기 버튼: 대화 기록이 있을 때(done/stopped/running) 표시
  const mdBtn = document.getElementById('sim-export-md-btn');
  if (mdBtn) mdBtn.classList.toggle('sim-hidden', status === 'idle');
  // 오류 배지는 로그가 비어있지 않으면 어떤 상태에서든 보인다 —
  // 'error'로 끝나지 않고 완주해도 중간에 실패한 턴은 확인할 수 있어야 한다.
  renderErrorIndicator();
}

export async function startSimulation() {
  readConfigFromUI();

  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.length) { alert('에이전트를 하나 이상 추가하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

  // 새 실행이므로 이전 실행의 오류 로그는 버린다.
  // (이어서 실행은 같은 실행의 연장이라 유지한다 — continueSimulation은 비우지 않는다)
  clearErrorLog();

  document.getElementById('sim-feed').innerHTML =
    '<div id="sim-feed-empty">시뮬레이션 시작 중...</div>';
  // 피드를 비웠으니 wave 정렬 버퍼(디렉터 카드 보류분)도 함께 버린다.
  resetWaveCardBuffer();
  document.getElementById('sim-turn-text').textContent = '대기 중';
  document.getElementById('sim-progress-fill').style.width = '0%';
  renderAgentCards();
  initD3Graph();
  initLocationMap();

  const res = await fetch('/api/simulation/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenario_id:            sim.currentScenarioId || null,
      agents:                 sim.agents,
      background:             sim.background,
      start_agent:            sim.start_agent,
      max_waves:              sim.max_waves,
      // 목표 기간(분). "사용 안 함"은 반드시 null — 0/음수는 백엔드가 422로 거부한다.
      target_duration_minutes: normalizeTargetDuration(sim.target_duration_minutes),
      step_delay:             sim.step_delay,
      token_limit:            sim.token_limit,
      llm_max_tokens:         sim.llm_max_tokens,
      extra_fields:           sim.extra_fields,
      events:                 sim.events,
      output_format_override: sim.output_format_override || '',
      sim_start_time:         sim.sim_start_time    || '09:00',
      sim_start_weekday:      normalizeWeekday(sim.sim_start_weekday),
      time_per_wave:          sim.time_per_wave    ?? 30,
      time_mode:              sim.time_mode ?? 'fixed',
      time_categories:        (sim.time_categories?.length ? sim.time_categories : DEFAULT_TIME_CATEGORIES),
      idle_minutes_schedule:  (sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE),
      max_scene_jump_minutes:   sim.max_scene_jump_minutes   ?? 45,
      max_daytime_jump_minutes: sim.max_daytime_jump_minutes ?? 180,
      max_silence_waves:      sim.max_silence_waves  ?? 3,
      early_stop_enabled:     sim.early_stop_enabled ?? true,
      server_id:              sim.server_id || null,
      temperature:            normalizeTemperature(sim.temperature),
      system_agent:           sim.system_agent,
      lang_fix_enabled:       sim.lang_fix_enabled ?? true,
      lang_fix_retries:       sim.lang_fix_retries ?? 2,
      location_graph:         sim.location_graph || [],
      // 전염 확률은 0~1, 모든 분 값은 0~52560000이고 max >= min — 벗어나면 서버가 422로 거부한다.
      infection_model:        buildInfectionModel(sim.infection_model),
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`시작 실패: ${err.detail || '서버 오류'}`);
    return;
  }

  setStatus('running');
  connectSSE();
}

export async function stopSimulation() {
  await fetch('/api/simulation/stop', { method: 'POST' });
  setStatus('stopped');
  // 시작되지 않을 wave 의 보류 카드를 삼키지 않는다 (sse.js 의 simulation_end 와 같은 이유).
  flushPendingWaveCards(null);
  removeTypingIndicator();
  disconnectSSE();
}

export async function continueSimulation() {
  readConfigFromUI();
  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

  // 주의: 감염병 모델(infection_model)은 여기서 보내지 않는다 — SimContinueConfig에 그
  // 필드가 없고(backend/api/simulation/schemas.py), /continue는 메모리에 살아 있는
  // sim_obj를 그대로 이어 쓰기 때문이다. 즉 감염 설정은 /start 또는 /load 시점의
  // config 스냅샷이 계속 유효하며, 설정 화면에서 바꾼 값은 다음 /start부터 적용된다.
  // (여기에 실어 보내면 Pydantic이 조용히 무시해 "바꿨는데 안 먹는" 오해만 만든다.)
  const res = await fetch('/api/simulation/continue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_agent: sim.start_agent,
      max_waves:   sim.max_waves,
      // max_waves와 같은 "이번 이어서 실행" 예산 — 복원된 누적 경과와 무관하게 이만큼 더 진행한다.
      target_duration_minutes: normalizeTargetDuration(sim.target_duration_minutes),
      step_delay:  sim.step_delay,
      events:      sim.events,
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`이어서 실행 실패: ${err.detail || '서버 오류'}`);
    return;
  }

  setStatus('running');
  connectSSE();
}
