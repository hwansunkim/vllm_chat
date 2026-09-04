// frontend/js/sim/run/sse.js
// EventSource lifecycle for /api/simulation/stream.

import { sim } from '../state.js';
import {
  addFeedMessage, addSceneEventToFeed,
  addTypingIndicator, removeTypingIndicator,
  updateWaveIndicator, addDirectorCallCard, addInterventionCard, addWorldEventCard,
  addMovementCard, addAppearanceCard, addSituationCard, applyWaveTimeStr,
  addInfectionCard, addMeetingCard, addTimeJumpCard, flushPendingWaveCards,
} from './feed.js';
import { updateAgentCard, updateAgentLocation, updateAgentInfection,
         updateAgentMeetingBadge, getCardEl } from './cards.js';
import { addD3Edge, refreshInfectionStyles } from '../graph/d3.js';
import { moveAgentOnMap, updateAgentInfectionOnMap, setMeetingIntentOnMap } from '../map/d3.js';
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

  // 디렉터가 돈 사실 + 비용(프롬프트 토큰·소요 시간)을 개입 여부와 무관하게 표시.
  // 시야(digest_waves)를 키우며 성능 영향을 관측하는 용도.
  es.addEventListener('director_call', e => {
    const d = JSON.parse(e.data);
    addDirectorCallCard(d);
  });

  // 가변 시간 모드에서 이 wave 의 경과 분을 어떻게 정했는지(카테고리 / AI 추론 + 이유,
  // 최종 분, 클램프 여부). 카테고리 라벨·범위를 미세조정하려면 판정 결과가 보여야 한다.
  // fixed 모드와 침묵 강제 재투입 wave 에서는 엔진이 emit 하지 않는다.
  // director_call 과 마찬가지로 다음 wave_start 보다 먼저 도착할 수 있어 피드가 버퍼링한다.
  es.addEventListener('time_jump', e => {
    const d = JSON.parse(e.data);
    addTimeJumpCard(d);
  });

  // 디렉터 개입 / 세계 사건. 페이로드의 `wave` 는 "이 개입이 실제로 소비되는 wave"이고,
  // 엔진이 디렉터를 wave 루프 상단에서 돌리므로 해당 wave 의 wave_start 보다 **먼저**
  // 도착한다. 그래서 피드는 이 카드를 바로 붙이지 않고 그 wave 의 구분선 뒤로 미룬다
  // (feed.js 의 _appendWaveCard / flushPendingWaveCards).
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

  // 만남 lock(_meeting_intent)의 생성/해소. "누가 누구를 만나러 이동 중"을 노출한다.
  // move_to에 사람을 지목하는 시나리오에서만 발생하며, 그 외에는 이벤트가 0건이다.
  es.addEventListener('meeting_update', e => {
    const d = JSON.parse(e.data);
    addMeetingCard(d);            // 피드 한 줄
    updateAgentMeetingBadge(d);   // chaser 카드의 "→ 목표" 뱃지
    setMeetingIntentOnMap(d);     // 위치 지도의 점선 추격선
  });

  // 감염 모델의 상태 전이(시드/전파/회복). 엔진이 계산한 결과이며 LLM 판단이 아니다.
  es.addEventListener('infection_update', e => {
    const d = JSON.parse(e.data);
    updateAgentInfection(d);          // 카드 뱃지 + sim.agentInfection 갱신
    refreshInfectionStyles();         // 관계 그래프 노드
    updateAgentInfectionOnMap(d.agent); // 위치 지도 아바타
    addInfectionCard(d);              // 피드 한 줄
  });

  es.addEventListener('appearance_update', e => {
    const d = JSON.parse(e.data);
    addAppearanceCard(d);
  });

  es.addEventListener('simulation_end', e => {
    const d = JSON.parse(e.data);
    setStatus('done');
    // 마지막 wave 가 시작되기 전에 종료됐다면 보류 중인 디렉터 카드가 남는다.
    // 삼켜버리면 사용자는 개입이 있었다는 사실 자체를 잃으므로 전부 흘려보낸다.
    flushPendingWaveCards(null);
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
    flushPendingWaveCards(null);
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
