// frontend/js/sim/settings/events.js
// Scenario events editor (wave-keyed system messages / agent enter/exit).

import { sim, esc } from '../state.js';

const EVENT_LABELS = {
  system_message: '📢 시스템 메시지',
  agent_enter:    '🎭 에이전트 등장',
  agent_exit:     '🚪 에이전트 퇴장',
};

export function renderScenarioEvents() {
  const list = document.getElementById('sim-events-list');
  list.innerHTML = '';

  const sorted = sim.events
    .map((e, i) => ({ ...e, _i: i }))
    .sort((a, b) => a.wave - b.wave);

  sorted.forEach(({ _i: idx }) => {
    const ev = sim.events[idx];
    const row = document.createElement('div');
    row.className = 'sim-event-row';

    const agentOptions = sim.agents
      .map(a => `<option value="${esc(a.name)}" ${a.name === ev.agent ? 'selected' : ''}>${esc(a.icon)} ${esc(a.name)}</option>`)
      .join('');

    const isAgentEvent  = ev.type === 'agent_enter' || ev.type === 'agent_exit';
    const isSysMsgEvent = ev.type === 'system_message';

    row.innerHTML = `
      <div class="sim-event-top">
        <span class="sim-event-wave-label">Wave</span>
        <input class="sim-event-wave-input" type="number" min="0" max="99"
               data-idx="${idx}" data-field="wave" value="${ev.wave}"/>
        <select class="sim-event-type-select" data-idx="${idx}" data-field="type">
          ${Object.entries(EVENT_LABELS).map(([v, l]) =>
            `<option value="${v}" ${ev.type === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
        <button class="sim-event-del" data-idx="${idx}">✕</button>
      </div>
      ${isAgentEvent ? `
      <div class="sim-event-field">
        <label>에이전트</label>
        <select data-idx="${idx}" data-field="agent">${agentOptions}</select>
      </div>` : ''}
      ${isSysMsgEvent ? `
      <div class="sim-event-field">
        <label>대상</label>
        <input type="text" data-idx="${idx}" data-field="targets_str"
               value="${(ev.targets || ['all']).join(',')}" placeholder="all 또는 boss,lee"/>
      </div>` : ''}
      <div class="sim-event-field">
        <label>메시지</label>
        <input type="text" data-idx="${idx}" data-field="message"
               value="${esc(ev.message || '')}" placeholder="내레이터 메시지..."/>
      </div>
    `;
    list.appendChild(row);
  });

  list.querySelectorAll('[data-field]').forEach(el => {
    el.addEventListener('change', () => syncEventField(el));
    el.addEventListener('input',  () => {
      if (el.dataset.field === 'type') syncEventField(el);
    });
  });
  list.querySelectorAll('.sim-event-del').forEach(el => {
    el.addEventListener('click', () => {
      sim.events.splice(+el.dataset.idx, 1);
      renderScenarioEvents();
    });
  });
}

function syncEventField(el) {
  const idx   = +el.dataset.idx;
  const field = el.dataset.field;
  if (field === 'wave') {
    sim.events[idx].wave = parseInt(el.value) || 0;
  } else if (field === 'type') {
    sim.events[idx].type = el.value;
    renderScenarioEvents();
  } else if (field === 'targets_str') {
    sim.events[idx].targets = el.value.split(',').map(s => s.trim()).filter(Boolean);
    if (!sim.events[idx].targets.length) sim.events[idx].targets = ['all'];
  } else {
    sim.events[idx][field] = el.value;
  }
}
