// frontend/js/sim/run/cards.js
// Agent card rendering & live updates on the simulation run view.

import { sim, esc, emotionClass, fmtK, getAgentIcon } from '../state.js';
import { openAgentContext } from '../context.js';

export function renderAgentCards() {
  sim.agentEmotions = {};
  const container = document.getElementById('sim-agent-cards');
  container.innerHTML = '';
  sim.agents.forEach(agent => {
    const card = document.createElement('div');
    const inactive = agent.initial_active === false;
    card.className = `sim-agent-card${inactive ? ' inactive' : ''}`;
    // Use CSS.escape so non-ASCII / special-char agent names produce valid IDs/selectors.
    card.id = `simc-${CSS.escape(agent.name)}`;
    card.title = '클릭하면 컨텍스트 윈도우 확인';
    card.style.cursor = 'pointer';
    const displayLabel = agent.display_name
      ? `${esc(agent.display_name)}<small style="color:#94a3b8;font-weight:400"> (${esc(agent.name)})</small>`
      : esc(agent.name);
    const metaHtml = sim.extra_fields.map(f => {
      const cls = f.name === 'emotion'
        ? `sim-feed-badge ${emotionClass(f.default)}`
        : 'sim-feed-badge emotion-neutral';
      return `<span class="${cls}" id="simc-meta-${esc(f.name)}-${esc(agent.name)}">${esc(f.default)}</span>`;
    }).join('');
    const locHtml = agent.location
      ? `<span class="sim-card-location" id="simc-loc-${esc(agent.name)}">📍 ${esc(agent.location)}</span>`
      : `<span class="sim-card-location sim-hidden" id="simc-loc-${esc(agent.name)}"></span>`;

    card.innerHTML = `
      <div class="sim-card-header">
        <span class="sim-card-icon" id="simc-icon-${esc(agent.name)}">${esc(getAgentIcon(agent, 'neutral'))}</span>
        <span class="sim-card-name">${displayLabel}</span>
        ${locHtml}
      </div>
      <div class="sim-card-meta">${metaHtml}</div>
      <div class="sim-card-token-row">
        <div class="sim-card-token-bar-wrap">
          <div class="sim-card-token-bar-fill" id="simc-tok-${esc(agent.name)}" style="width:0%"></div>
        </div>
        <span class="sim-card-token-label" id="simc-tokl-${esc(agent.name)}">— / ${fmtK(sim.token_limit)}</span>
      </div>
      <div class="sim-card-preview" id="simc-pre-${esc(agent.name)}">대기 중...</div>
    `;
    card.addEventListener('click', () => openAgentContext(agent.name));
    container.appendChild(card);
  });
}

export function updateAgentCard(speaker, meta, promptTokens, tokenLimit, preview) {
  Object.entries(meta || {}).forEach(([field, value]) => {
    const el = document.getElementById(`simc-meta-${field}-${speaker}`);
    if (!el) return;
    el.textContent = value;
    if (field === 'emotion') {
      el.className = `sim-feed-badge ${emotionClass(String(value))}`;
      const iconEl = document.getElementById(`simc-icon-${speaker}`);
      if (iconEl) {
        const agent = sim.agents.find(a => a.name === speaker);
        if (agent) iconEl.textContent = getAgentIcon(agent, String(value));
      }
    }
  });

  if (promptTokens && tokenLimit) {
    const pct = Math.min(100, (promptTokens / tokenLimit) * 100);
    const barEl = document.getElementById(`simc-tok-${speaker}`);
    const lblEl = document.getElementById(`simc-tokl-${speaker}`);
    if (barEl) {
      barEl.style.width = `${pct}%`;
      barEl.className   = `sim-card-token-bar-fill${pct >= 90 ? ' danger' : pct >= 70 ? ' warn' : ''}`;
    }
    if (lblEl) lblEl.textContent = `${fmtK(promptTokens)} / ${fmtK(tokenLimit)}`;
  }

  const preEl = document.getElementById(`simc-pre-${speaker}`);
  if (preEl && preview) preEl.textContent = preview.slice(0, 42);
}

/** Update the location badge on an agent card after a move event. */
export function updateAgentLocation(agentName, location) {
  const el = document.getElementById(`simc-loc-${CSS.escape(agentName)}`);
  if (!el) return;
  if (location) {
    el.textContent = `📍 ${location}`;
    el.classList.remove('sim-hidden');
  }
}

/** Lookup the live card element by agent name, handling special characters. */
export function getCardEl(name) {
  return document.getElementById(`simc-${CSS.escape(name)}`);
}
