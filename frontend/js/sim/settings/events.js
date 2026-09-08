// frontend/js/sim/settings/events.js
// Scenario events editor (wave-keyed system messages / agent enter/exit).

import { sim, esc } from '../state.js';
import { updateSectionBadges } from './sections.js';

const EVENT_LABELS = {
  system_message: '📢 시스템 메시지',
  agent_enter:    '🎭 에이전트 등장',
  agent_exit:     '🚪 에이전트 퇴장',
  infect_agent:   '🦠 감염 시드 / 환자 0번',
};

// 에이전트 선택 드롭다운이 필요한 타입 (agent 필드를 쓴다).
const AGENT_EVENT_TYPES = ['agent_enter', 'agent_exit', 'infect_agent'];

// Build the ordered list of selectable targets for system_message events.
function _buildTargetOptions() {
  const opts = [{ value: 'all', label: '전체', cls: 'evt-target-all' }];
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

// 이벤트 트리거: wave 번호 또는 시계 시각(at_time). at_time 이 있으면 그게 우선.
const eventTrigger = ev => (ev.at_time ? 'time' : 'wave');

export function renderScenarioEvents() {
  const list = document.getElementById('sim-events-list');
  list.innerHTML = '';
  updateSectionBadges(sim);   // 이벤트 개수가 섹션 헤더 뱃지에 그대로 노출된다

  const sorted = sim.events
    .map((e, i) => ({ ...e, _i: i }))
    .sort((a, b) => {
      const at = a.at_time || '', bt = b.at_time || '';
      if (at && bt) return at.localeCompare(bt);
      if (at) return 1;          // 시각 트리거는 wave 트리거 뒤에
      if (bt) return -1;
      return a.wave - b.wave;
    });

  sorted.forEach(({ _i: idx }) => {
    const ev = sim.events[idx];
    const row = document.createElement('div');
    row.className = 'sim-event-row';

    // 드롭다운은 선택값이 없으면 첫 항목이 선택된 채로 그려진다. 상태가 빈 문자열(또는
    // 삭제된 에이전트)이면 화면엔 이름이 보이는데 실제로는 agent=""가 전송돼 서버가
    // 무시하므로, 그리기 전에 상태를 화면과 같은 값으로 맞춘다.
    _syncAgentSelection(idx);

    const agentOptions = sim.agents
      .map(a => `<option value="${esc(a.name)}" ${a.name === ev.agent ? 'selected' : ''}>${esc(a.icon)} ${esc(a.display_name || a.name)}</option>`)
      .join('');

    const isAgentEvent  = AGENT_EVENT_TYPES.includes(ev.type);
    const isSysMsgEvent = ev.type === 'system_message';
    // 감염 시드는 targets를 쓰지 않고(위 조건에서 이미 숨겨진다), message도 관전용이다.
    const isInfectEvent = ev.type === 'infect_agent';
    // 모델이 꺼져 있으면 서버가 이 이벤트를 조용히 무시하므로 미리 알린다.
    const infectionOff  = isInfectEvent && !sim.infection_model?.enabled;

    const trigger = eventTrigger(ev);
    row.innerHTML = `
      <div class="sim-event-top">
        <select class="sim-event-trigger-select" data-idx="${idx}" data-field="_trigger">
          <option value="wave" ${trigger === 'wave' ? 'selected' : ''}>Wave</option>
          <option value="time" ${trigger === 'time' ? 'selected' : ''}>시각</option>
        </select>
        ${trigger === 'time' ? `
        <input class="sim-event-time-input" type="time"
               data-idx="${idx}" data-field="at_time" value="${esc(ev.at_time || '16:00')}"/>` : `
        <input class="sim-event-wave-input" type="number" min="0" max="99"
               data-idx="${idx}" data-field="wave" value="${ev.wave}"/>`}
        <select class="sim-event-type-select" data-idx="${idx}" data-field="type">
          ${Object.entries(EVENT_LABELS).map(([v, l]) =>
            `<option value="${v}" ${ev.type === v ? 'selected' : ''}>${l}</option>`
          ).join('')}
        </select>
        <button class="sim-event-del" data-idx="${idx}">✕</button>
      </div>
      ${trigger === 'time' ? `
      <div class="sim-event-hint">
        시뮬레이션 시계가 이 시각에 도달한 첫 wave에 발동합니다. 시간 추론이 이 시각을
        넘겨 점프하지 않습니다(예: “짱구 태권도 16:00”이 큰 시간 점프에 스킵되는 것 방지).
        시간 모드가 켜져 있어야 의미가 있습니다.
      </div>` : ''}
      ${infectionOff ? `
      <div class="sim-event-warn">
        ⚠ 감염병 모델이 꺼져 있어 이 이벤트는 실행되지 않습니다 — 위 “🦠 감염병 모델” 섹션에서 활성화하세요.
      </div>` : ''}
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
               value="${esc(ev.message || '')}"
               placeholder="${isInfectEvent ? '관전용 메모 (선택)...' : '내레이터 메시지...'}"/>
      </div>
      ${isInfectEvent ? `
      <div class="sim-event-hint">
        이 문구는 에이전트에게 전달되지 않습니다 (관전자 전용) — 감염 사실은 오직 증상 서사로만 인지합니다.
      </div>` : ''}
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

/**
 * 에이전트 선택이 필요한 타입인데 상태의 agent가 비었거나 목록에 없으면 첫 에이전트로 맞춘다.
 * (agent_enter/agent_exit는 서버가 경고만 남기지만, infect_agent는 agent가 필수다.)
 */
function _syncAgentSelection(idx) {
  const ev = sim.events[idx];
  if (!AGENT_EVENT_TYPES.includes(ev.type) || !sim.agents.length) return;
  if (!sim.agents.some(a => a.name === ev.agent)) ev.agent = sim.agents[0].name;
}

function syncEventField(el) {
  const idx   = +el.dataset.idx;
  const field = el.dataset.field;
  if (field === '_trigger') {
    // wave ↔ 시각 전환. 한쪽 값만 유효하도록 다른 쪽은 비운다.
    if (el.value === 'time') {
      sim.events[idx].at_time = sim.events[idx].at_time || '16:00';
    } else {
      sim.events[idx].at_time = '';
    }
    renderScenarioEvents();
  } else if (field === 'wave') {
    sim.events[idx].wave = parseInt(el.value) || 0;
  } else if (field === 'at_time') {
    sim.events[idx].at_time = el.value;   // <input type="time"> → "HH:MM" 또는 ""
  } else if (field === 'type') {
    sim.events[idx].type = el.value;
    _syncAgentSelection(idx);
    renderScenarioEvents();
  } else {
    sim.events[idx][field] = el.value;
  }
}
