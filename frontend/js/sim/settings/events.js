// frontend/js/sim/settings/events.js
// Scenario events editor (wave-keyed system messages / agent enter/exit).

import { sim, esc, getAllGroups } from '../state.js';

const EVENT_LABELS = {
  system_message: '📢 시스템 메시지',
  agent_enter:    '🎭 에이전트 등장',
  agent_exit:     '🚪 에이전트 퇴장',
};

// Build the ordered list of selectable targets for system_message events.
function _buildTargetOptions() {
  const opts = [{ value: 'all', label: '전체', cls: 'evt-target-all' }];
  for (const g of getAllGroups()) {
    opts.push({ value: `group:${g}`, label: `그룹 ${g}`, cls: 'evt-target-group' });
  }
  for (const a of sim.agents) {
    opts.push({ value: a.name, label: `${a.icon} ${a.name}`, cls: 'evt-target-agent' });
  }
  return opts;
}

function _renderTargetChips(idx, currentTargets) {
  const targets = currentTargets && currentTargets.length ? currentTargets : ['all'];
  return _buildTargetOptions().map(opt => {
    const sel = targets.includes(opt.value);
    return `<span class="evt-target-chip ${opt.cls}${sel ? ' selected' : ''}"
                  data-idx="${idx}" data-value="${esc(opt.value)}">${esc(opt.label)}</span>`;
  }).join('');
}

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
      .map(a => `<option value="${esc(a.name)}" ${a.name === ev.agent ? 'selected' : ''}>${esc(a.icon)} ${esc(a.display_name || a.name)}</option>`)
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
      <div class="sim-event-targets-row">
        <span class="sim-event-targets-label">대상</span>
        <div class="evt-target-chips" data-idx="${idx}">
          ${_renderTargetChips(idx, ev.targets)}
        </div>
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

  // Target chip toggle
  list.querySelectorAll('.evt-target-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const idx = +chip.dataset.idx;
      const val = chip.dataset.value;
      let targets = [...(sim.events[idx].targets || ['all'])];

      if (val === 'all') {
        targets = ['all'];
      } else {
        targets = targets.filter(t => t !== 'all');
        if (targets.includes(val)) {
          targets = targets.filter(t => t !== val);
        } else {
          targets.push(val);
        }
        if (!targets.length) targets = ['all'];
      }
      sim.events[idx].targets = targets;
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
  } else {
    sim.events[idx][field] = el.value;
  }
}
