// frontend/js/sim/run/control.js
// Start / stop / continue / status-badge logic for simulation runs.

import { sim, normalizeTargetDuration } from '../state.js';
import { buildSimConfig } from '../config.js';
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
  const labels = { idle: '대기 중', running: '실행 중', stopping: '중지 중…',
                   loading: '불러오는 중…', done: '완료', stopped: '중지됨', error: '오류' };
  badge.textContent = labels[status] || status;
  badge.className   = `sim-status-badge ${status}`;
  // 'stopping'/'loading' 은 워커가 아직 살아 있는 과도기 — 'running' 과 똑같이
  // 새 실행·이어서 실행을 막는다(백엔드도 _BUSY_STATES 로 거부한다).
  const busy = ['running', 'stopping', 'loading'].includes(status);
  document.getElementById('sim-start-btn').disabled    = busy;
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
    // 필드 목록은 저장 경로와 같은 buildSimConfig() 한 곳에만 있다(../config.js).
    // scenario_id 만 실행 전용이라 여기서 붙인다 — 실행 이력을 시나리오에 연결하는 키.
    body: JSON.stringify({
      scenario_id: sim.currentScenarioId || null,
      ...buildSimConfig(),
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
  const res  = await fetch('/api/simulation/stop', { method: 'POST' });
  const data = await res.json().catch(() => ({ status: 'stopped' }));
  // 백엔드는 워커가 현재 wave 를 마치고 종료할 때까지 기다렸다 실제 상태를 준다.
  // 대개 'stopped'/'done' 이 바로 온다. 긴 LLM 호출에 걸려 'stopping' 이면
  // /status 로 확정될 때까지 폴링한다 — 그동안 "이어서 실행" 은 비활성.
  const terminal = ['done', 'stopped', 'error'];
  setStatus(terminal.includes(data.status) ? data.status : 'stopping');
  // 시작되지 않을 wave 의 보류 카드를 삼키지 않는다 (sse.js 의 simulation_end 와 같은 이유).
  flushPendingWaveCards(null);
  removeTypingIndicator();
  disconnectSSE();

  if (!terminal.includes(sim.status)) {
    for (let i = 0; i < 20 && !terminal.includes(sim.status); i++) {
      await new Promise(r => setTimeout(r, 1500));
      try {
        const s = await (await fetch('/api/simulation/status')).json();
        if (terminal.includes(s.status)) { setStatus(s.status); break; }
      } catch { /* 다음 폴링에서 재시도 */ }
    }
    if (!terminal.includes(sim.status)) setStatus('stopped');  // 최종 폴백
  }
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
