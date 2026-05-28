// frontend/js/sim/run/feed.js
// Live feed: typing indicator, agent messages, scene events, wave indicator.

import { sim, esc, emotionClass } from '../state.js';

export function removeFeedEmpty() {
  const el = document.getElementById('sim-feed-empty');
  if (el) el.remove();
}

export function addTypingIndicator(speaker) {
  if (document.getElementById(`sim-typing-${speaker}`)) return;
  removeFeedEmpty();
  const agent = sim.agents.find(a => a.name === speaker) || { icon: '🤖', name: speaker };
  const el = document.createElement('div');
  el.id = `sim-typing-${speaker}`;
  el.className = 'sim-typing-row';
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.name)}</span>
    </div>
    <div class="sim-typing-bubble">
      <div class="sim-typing-dots"><span></span><span></span><span></span></div>
      <span class="sim-typing-label">생성 중...</span>
    </div>
  `;
  const feed = document.getElementById('sim-feed');
  feed.appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function removeTypingIndicator(speaker) {
  if (speaker) {
    document.getElementById(`sim-typing-${speaker}`)?.remove();
  } else {
    document.querySelectorAll('[id^="sim-typing-"]').forEach(el => el.remove());
  }
}

export function addFeedMessage(data) {
  removeTypingIndicator(data.speaker);
  const agent = sim.agents.find(a => a.name === data.speaker) || { icon: '🤖', name: data.speaker };
  const targets = data.targets.filter(t => t !== 'system');
  const targetStr = targets.length
    ? (targets.includes('all') ? '→ (전체)' : `→ ${targets.join(', ')}`)
    : '(독백)';
  const el = document.createElement('div');
  el.className = 'sim-feed-msg';
  const meta = data.meta || {};
  const metaBadges = Object.entries(meta).map(([k, v]) =>
    `<span class="sim-feed-badge ${k === 'emotion' ? emotionClass(String(v)) : 'emotion-neutral'}">${esc(String(v))}</span>`
  ).join('');

  const actionNote = data.action_note || '';
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.name)}</span>
      <span class="sim-feed-target">${esc(targetStr)}</span>
    </div>
    <div class="sim-feed-bubble">${esc(data.content)}</div>
    ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
    ${metaBadges ? `<div class="sim-feed-meta">${metaBadges}</div>` : ''}
    ${data.reasoning_preview
      ? `<div class="sim-feed-thinking">🧠 ${esc(data.reasoning_preview)}...</div>`
      : ''}
  `;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addSceneEventToFeed(d) {
  removeFeedEmpty();
  const icons  = { system_message: '📢', agent_enter: '🎭', agent_exit: '🚪' };
  const labels = { system_message: '시스템', agent_enter: '등장', agent_exit: '퇴장' };
  const icon  = icons[d.event_type]  || '📌';
  const label = labels[d.event_type] || d.event_type;
  const agentHint = d.agent ? ` (${d.agent})` : '';
  const el = document.createElement('div');
  el.className = 'sim-scene-event';
  el.innerHTML = `
    <div class="sim-scene-event-icon">${icon}</div>
    <div class="sim-scene-event-body">
      <div class="sim-scene-event-type">${label}${esc(agentHint)}</div>
      <div class="sim-scene-event-msg">${esc(d.message || '')}</div>
    </div>
  `;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function updateWaveIndicator(waveNum, agents) {
  document.getElementById('sim-turn-text').textContent =
    `Wave ${waveNum}  |  ${agents.join(', ')}`;
  document.getElementById('sim-progress-fill').style.width = '30%';
}
