// frontend/js/sim/run/sse.js
// EventSource lifecycle for /api/simulation/stream.

import { sim } from '../state.js';
import {
  addFeedMessage, addSceneEventToFeed,
  addTypingIndicator, removeTypingIndicator,
  updateWaveIndicator, addSummaryCard, addInterventionCard, addWorldEventCard,
  addMovementCard, addAppearanceCard, addSituationCard, applyWaveTimeStr,
} from './feed.js';
import { updateAgentCard, updateAgentLocation, getCardEl } from './cards.js';
import { addD3Edge } from '../graph/d3.js';
import { moveAgentOnMap } from '../map/d3.js';
import { setStatus } from './control.js';
import { recordTurnError, recordConnectionError } from './errors.js';
import { fetchAgentContext } from '../context.js';

// simulation_end 이벤트의 end_reason → 화면 문구. 구버전 백엔드는 이 필드를 보내지 않으므로
// 값이 없거나 모르는 값이면 종료 사유 없이 기존 문구만 표시한다.
const END_REASON_LABELS = {
  max_waves:       '최대 wave 수 도달로 종료',
  target_duration: '목표 기간 도달로 종료',
  silence:         '대화가 끊겨 조기 종료',
  no_agents:       '활성 에이전트가 없어 종료',
  stopped:         '사용자 중지로 종료',
};

export function connectSSE() {
  disconnectSSE();

  const es = new EventSource('/api/simulation/stream');
  sim.eventSource = es;

  es.addEventListener('wave_start', e => {
    const d = JSON.parse(e.data);
    removeTypingIndicator();
    document.querySelectorAll('.sim-agent-card').forEach(c => c.classList.remove('speaking'));
    d.agents.forEach(name => {
      getCardEl(name)?.classList.add('speaking');
    });
    updateWaveIndicator(d.wave, d.agents);
  });

  es.addEventListener('turn_situation', e => {
    const d = JSON.parse(e.data);
    addSituationCard(d);
  });

  es.addEventListener('turn_start', e => {
    const d = JSON.parse(e.data);
    addTypingIndicator(d.speaker);
  });

  es.addEventListener('turn_complete', e => {
    const d = JSON.parse(e.data);
    sim.agentEmotions[d.speaker] = (d.meta || {}).emotion || 'neutral';
    if (d.time_str) applyWaveTimeStr(d.wave, d.time_str);
    addFeedMessage(d);
    updateAgentCard(d.speaker, d.meta || {}, d.prompt_tokens, d.token_limit, d.content);
    d.new_edges?.forEach(edge =>
      addD3Edge(edge.source, edge.target, edge.emotion || (edge.meta || {}).emotion || 'neutral')
    );
    getCardEl(d.speaker)?.classList.remove('speaking');
    if (sim.selectedAgent === d.speaker &&
        !document.getElementById('sim-tab-context').classList.contains('sim-hidden')) {
      fetchAgentContext(d.speaker);
    }
  });

  es.addEventListener('turn_error', e => {
    const d = JSON.parse(e.data);
    // payload의 turn/speaker/error를 그대로 누적한다 — 실행이 막힐 때 원인을 볼 수 있는
    // 유일한 정보원이므로 버리지 않는다. (상태 배지 옆 "⚠ N건" 팝업에서 조회)
    recordTurnError(d);
    removeTypingIndicator(d.speaker);
    getCardEl(d.speaker)?.classList.remove('speaking');
  });

  es.addEventListener('scene_event', e => {
    const d = JSON.parse(e.data);
    addSceneEventToFeed(d);
    if (d.event_type === 'agent_enter') {
      getCardEl(d.agent)?.classList.remove('inactive');
    } else if (d.event_type === 'agent_exit') {
      const card = getCardEl(d.agent);
      if (card) { card.classList.remove('speaking'); card.classList.add('exited'); }
    }
  });

  es.addEventListener('wave_summary', e => {
    const d = JSON.parse(e.data);
    addSummaryCard(d);
  });

  es.addEventListener('system_intervention', e => {
    const d = JSON.parse(e.data);
    addInterventionCard(d);
  });

  es.addEventListener('world_event', e => {
    const d = JSON.parse(e.data);
    addWorldEventCard(d);
  });

  es.addEventListener('agent_move', e => {
    const d = JSON.parse(e.data);
    addMovementCard(d);
    updateAgentLocation(d.agent, d.to);
    moveAgentOnMap(d.agent, d.to);
  });

  es.addEventListener('appearance_update', e => {
    const d = JSON.parse(e.data);
    addAppearanceCard(d);
  });

  es.addEventListener('simulation_end', e => {
    const d = JSON.parse(e.data);
    setStatus('done');
    removeTypingIndicator();
    document.querySelectorAll('.sim-agent-card').forEach(c => c.classList.remove('speaking'));
    const reason = END_REASON_LABELS[d.end_reason];
    document.getElementById('sim-turn-text').textContent =
      `완료  |  총 ${d.total_turns}턴${reason ? `  |  ${reason}` : ''}`;
    document.getElementById('sim-progress-fill').style.width = '100%';
    es.close();
    sim.eventSource = null;
  });

  es.addEventListener('error', () => {
    // EventSource의 error 이벤트에는 브라우저 스펙상 메시지 데이터가 없다.
    // 고정 문구만 같은 로그에 남겨 turn_error들과 시간순으로 함께 보이게 한다.
    recordConnectionError();
    removeTypingIndicator();
    setStatus('error');
    es.close();
    sim.eventSource = null;
  });

  es.addEventListener('ping', () => {});
}

export function disconnectSSE() {
  if (sim.eventSource) {
    sim.eventSource.close();
    sim.eventSource = null;
  }
}
