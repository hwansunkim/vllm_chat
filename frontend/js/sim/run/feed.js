// frontend/js/sim/run/feed.js
// Live feed: typing indicator, agent messages, scene events, wave indicator.

import { sim, esc, emotionClass, agentLabel } from '../state.js';

export function renderHistoricalFeed(entries) {
  const feed = document.getElementById('sim-feed');
  feed.innerHTML = '';

  if (!entries || !entries.length) {
    feed.innerHTML = '<div id="sim-feed-empty">저장된 대화 기록이 없습니다.</div>';
    return;
  }

  entries.forEach(entry => {
    const agent = sim.agents.find(a => a.name === entry.speaker) || { icon: '🤖', name: entry.speaker };
    const targets = (entry.targets || []).filter(t => t !== 'self' && t !== 'system');
    const targetLabels = targets.map(t => t === 'all' ? '전체' : agentLabel(t));
    const targetStr = targets.length ? `→ ${targetLabels.join(', ')}` : '(독백)';

    const meta = entry.meta || {};
    const metaBadges = Object.entries(meta)
      .filter(([k]) => k !== 'action_note')
      .map(([k, v]) =>
        `<span class="sim-feed-badge ${k === 'emotion' ? emotionClass(String(v)) : 'emotion-neutral'}">${esc(String(v))}</span>`)
      .join('');
    const actionNote = entry.action_note || '';
    const waveBadge  = entry.wave != null ? `<span class="sim-feed-wave-mini">W${entry.wave}</span>` : '';

    const div = document.createElement('div');
    div.className = 'sim-feed-msg sim-feed-msg-history';
    div.innerHTML = `
      <div class="sim-feed-header">
        <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.display_name || agent.name)}</span>
        ${waveBadge}
        <span class="sim-feed-target">${esc(targetStr)}</span>
      </div>
      <div class="sim-feed-bubble">${esc(entry.content)}</div>
      ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
      ${metaBadges ? `<div class="sim-feed-meta">${metaBadges}</div>` : ''}
    `;
    feed.appendChild(div);
  });

  feed.lastElementChild?.scrollIntoView({ block: 'end' });
}

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
      <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.display_name || agent.name)}</span>
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
  const targets = data.targets.filter(t => t !== 'self' && t !== 'system');
  const targetLabels = targets.map(t => {
    if (t === 'all') return '전체';
    return agentLabel(t);
  });
  const targetStr = targets.length
    ? `→ ${targetLabels.join(', ')}`
    : '(독백)';
  const el = document.createElement('div');
  el.className = 'sim-feed-msg';
  const meta = data.meta || {};
  const metaBadges = Object.entries(meta)
    .filter(([k]) => k !== 'action_note')
    .map(([k, v]) =>
      `<span class="sim-feed-badge ${k === 'emotion' ? emotionClass(String(v)) : 'emotion-neutral'}">${esc(String(v))}</span>`
    ).join('');

  const actionNote = data.action_note || '';
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.display_name || agent.name)}</span>
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

export function addInterventionCard(d) {
  removeFeedEmpty();
  const targetLabel = d.target_alias || d.target;
  const el          = document.createElement('div');
  el.className      = 'sim-intervention-card';
  el.innerHTML = `
    <div class="sim-intervention-header">
      <span class="sim-intervention-icon">${esc(d.icon || '🎬')}</span>
      <span class="sim-intervention-name">${esc(d.display_name || '내레이터')}</span>
      <span class="sim-intervention-arrow">→</span>
      <span class="sim-intervention-target">${esc(targetLabel)}</span>
      <span class="sim-intervention-wave">W${d.wave}</span>
    </div>
    <div class="sim-intervention-msg">${esc(d.message || '')}</div>
    ${d.reason ? `<div class="sim-intervention-reason">${esc(d.reason)}</div>` : ''}`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addSummaryCard(d) {
  removeFeedEmpty();
  const range      = d.wave_start === d.wave_end ? `Wave ${d.wave_start}` : `Wave ${d.wave_start}–${d.wave_end}`;
  const keyEvents  = (d.key_events || []).map(e => `<li>${esc(e)}</li>`).join('');
  const el         = document.createElement('div');
  el.className     = 'sim-summary-card';
  el.innerHTML = `
    <div class="sim-summary-header">
      <span class="sim-summary-icon">📊</span>
      <span class="sim-summary-range">${esc(range)} 요약</span>
      ${d.mood ? `<span class="sim-summary-mood">${esc(d.mood)}</span>` : ''}
    </div>
    <div class="sim-summary-body">
      <p class="sim-summary-text">${esc(d.summary || '')}</p>
      ${keyEvents ? `<ul class="sim-summary-events">${keyEvents}</ul>` : ''}
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addWorldEventCard(d) {
  removeFeedEmpty();
  const targetStr = (d.target_aliases || d.targets || []).join(', ') || '전체';
  const el = document.createElement('div');
  el.className = 'sim-world-event-card';
  el.innerHTML = `
    <div class="sim-world-event-header">
      <span class="sim-world-event-icon">🌍</span>
      <span class="sim-world-event-label">세계 사건</span>
      <span class="sim-world-event-targets">→ ${esc(targetStr)}</span>
      <span class="sim-world-event-wave">W${d.wave}</span>
    </div>
    <div class="sim-world-event-content">${esc(d.content || '')}</div>
    ${d.reason ? `<div class="sim-world-event-reason">${esc(d.reason)}</div>` : ''}`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addSceneEventToFeed(d) {
  removeFeedEmpty();
  const icons  = { system_message: '📢', agent_enter: '🎭', agent_exit: '🚪' };
  const labels = { system_message: '시스템', agent_enter: '등장', agent_exit: '퇴장' };
  const icon  = icons[d.event_type]  || '📌';
  const label = labels[d.event_type] || d.event_type;
  const agentHint = d.agent ? ` (${agentLabel(d.agent)})` : '';
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

export function addMovementCard(d) {
  removeFeedEmpty();
  const fromStr = d.from ? esc(d.from) : '(미설정)';
  const toStr   = esc(d.to || '');
  const el      = document.createElement('div');
  el.className  = 'sim-movement-card';
  el.innerHTML = `
    <div class="sim-movement-header">
      <span class="sim-movement-icon">🚶</span>
      <span class="sim-movement-name">${esc(d.display_name || d.agent)}</span>
      <span class="sim-movement-arrow">이동</span>
      <span class="sim-movement-route">${fromStr} → ${toStr}</span>
      <span class="sim-movement-wave">W${d.wave}</span>
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addAppearanceCard(d) {
  removeFeedEmpty();
  const el     = document.createElement('div');
  el.className = 'sim-appearance-card';
  el.innerHTML = `
    <div class="sim-appearance-header">
      <span class="sim-appearance-icon">🪞</span>
      <span class="sim-appearance-name">${esc(d.display_name || d.agent)}</span>
      <span class="sim-appearance-label">외모 변경</span>
      <span class="sim-appearance-wave">W${d.wave}</span>
    </div>
    <div class="sim-appearance-desc">${esc(d.description || '')}</div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addSituationCard(d) {
  removeFeedEmpty();
  const agent  = sim.agents.find(a => a.name === d.agent) || { icon: '📍', name: d.agent };
  const el     = document.createElement('div');
  el.className = 'sim-situation-card';
  el.innerHTML = `
    <div class="sim-situation-header">
      <span class="sim-situation-icon">📍</span>
      <span class="sim-situation-name">${esc(agent.display_name || agent.name)}</span>
      <span class="sim-situation-label">상황 컨텍스트</span>
      <span class="sim-situation-wave">W${d.wave}</span>
      <span class="sim-situation-toggle">▶</span>
    </div>
    <pre class="sim-situation-body sim-hidden">${esc(d.text)}</pre>
  `;
  const header = el.querySelector('.sim-situation-header');
  const body   = el.querySelector('.sim-situation-body');
  const toggle = el.querySelector('.sim-situation-toggle');
  header.addEventListener('click', () => {
    const hidden = body.classList.toggle('sim-hidden');
    toggle.textContent = hidden ? '▶' : '▼';
  });
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function updateWaveIndicator(waveNum, agents) {
  const labels = agents.map(k => agentLabel(k));
  document.getElementById('sim-turn-text').textContent =
    `Wave ${waveNum}  |  ${labels.join(', ')}`;
  document.getElementById('sim-progress-fill').style.width = '30%';
}
