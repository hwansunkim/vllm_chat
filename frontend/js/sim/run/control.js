// frontend/js/sim/run/control.js
// Start / stop / continue / status-badge logic for simulation runs.

import { sim, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday,
         normalizeTemperature, normalizeTargetDuration } from '../state.js';
import { readConfigFromUI } from '../settings/page.js';
import { renderAgentCards } from './cards.js';
import { removeTypingIndicator } from './feed.js';
import { initD3Graph } from '../graph/d3.js';
import { initLocationMap } from '../map/d3.js';
import { connectSSE, disconnectSSE } from './sse.js';

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
}

export async function startSimulation() {
  readConfigFromUI();

  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.length) { alert('에이전트를 하나 이상 추가하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

  document.getElementById('sim-feed').innerHTML =
    '<div id="sim-feed-empty">시뮬레이션 시작 중...</div>';
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
      output_format_template: sim.output_format_template || '',
      summary_interval:       sim.summary_interval || 0,
      sim_start_time:         sim.sim_start_time    || '09:00',
      sim_start_weekday:      normalizeWeekday(sim.sim_start_weekday),
      time_per_wave:          sim.time_per_wave    ?? 30,
      time_mode:              sim.time_mode ?? 'fixed',
      time_categories:        (sim.time_categories?.length ? sim.time_categories : DEFAULT_TIME_CATEGORIES),
      idle_minutes_schedule:  (sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE),
      max_silence_waves:      sim.max_silence_waves  ?? 3,
      early_stop_enabled:     sim.early_stop_enabled ?? true,
      server_id:              sim.server_id || null,
      temperature:            normalizeTemperature(sim.temperature),
      system_agent:           sim.system_agent,
      lang_fix_enabled:       sim.lang_fix_enabled ?? true,
      lang_fix_retries:       sim.lang_fix_retries ?? 2,
      location_graph:         sim.location_graph || [],
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
  removeTypingIndicator();
  disconnectSSE();
}

export async function continueSimulation() {
  readConfigFromUI();
  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

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
