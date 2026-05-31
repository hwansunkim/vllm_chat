// frontend/js/sim/run/sse.js
// EventSource lifecycle for /api/simulation/stream.

import { sim } from '../state.js';
import {
  addFeedMessage, addSceneEventToFeed,
  addTypingIndicator, removeTypingIndicator,
  updateWaveIndicator, addSummaryCard, addInterventionCard, addWorldEventCard,
  addMovementCard, addAppearanceCard, addSituationCard,
} from './feed.js';
import { updateAgentCard, updateAgentLocation, getCardEl } from './cards.js';
import { addD3Edge } from '../graph/d3.js';
import { setStatus } from './control.js';
import { fetchAgentContext } from '../context.js';

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
    document.getElementById('sim-turn-text').textContent =
      `완료  |  총 ${d.total_turns}턴`;
    document.getElementById('sim-progress-fill').style.width = '100%';
    es.close();
    sim.eventSource = null;
  });

  es.addEventListener('error', () => {
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
