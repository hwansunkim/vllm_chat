// frontend/js/sim/run/control.js
// Start / stop / continue / status-badge logic for simulation runs.

import { sim } from '../state.js';
import { readConfigFromUI } from '../settings/page.js';
import { renderAgentCards } from './cards.js';
import { removeTypingIndicator } from './feed.js';
import { initD3Graph } from '../graph/d3.js';
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

  const res = await fetch('/api/simulation/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenario_id:            sim.currentScenarioId || null,
      agents:                 sim.agents,
      background:             sim.background,
      start_agent:            sim.start_agent,
      max_waves:              sim.max_waves,
      step_delay:             sim.step_delay,
      token_limit:            sim.token_limit,
      llm_max_tokens:         sim.llm_max_tokens,
      extra_fields:           sim.extra_fields,
      events:                 sim.events,
      output_format_template: sim.output_format_template || '',
      summary_interval:       sim.summary_interval || 0,
      sim_start_time:         sim.sim_start_time    || '09:00',
      time_per_wave:          sim.time_per_wave    ?? 30,
      max_silence_waves:      sim.max_silence_waves  ?? 3,
      early_stop_enabled:     sim.early_stop_enabled ?? true,
      server_id:              sim.server_id || null,
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
