// frontend/js/sim/settings/agents.js
// Accordion editor for the agents list (settings view).

import { sim, esc, _expandedAgents, getAllGroups, detectGender, getAgentIcon } from '../state.js';
import { renderScenarioEvents } from './events.js';

export function renderStartAgentSelect() {
  const sel  = document.getElementById('sim-start-agent');
  const prev = sel.value;
  sel.innerHTML = '';
  sim.agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = `${getAgentIcon(a, 'neutral')} ${a.display_name || a.name}`;
    sel.appendChild(opt);
  });
  if (prev && sim.agents.find(a => a.name === prev)) sel.value = prev;
  else if (sim.agents.length) sel.value = sim.agents[0].name;
  sim.start_agent = sel.value;
}

export function renderAgentListInConfig() {
  const list = document.getElementById('sim-agent-list');
  list.innerHTML = '';

  sim.agents.forEach((agent, idx) => {
    const isActive   = agent.initial_active !== false;
    const isExpanded = _expandedAgents.has(agent.name);

    const row = document.createElement('div');
    row.className = 'sim-acrd-row';

    // ── Header ──
    const header = document.createElement('div');
    header.className = 'sim-acrd-header';
    header.innerHTML = `
      <span class="sim-acrd-icon">${esc(getAgentIcon(agent, 'neutral'))}</span>
      <div class="sim-acrd-names">
        <span class="sim-acrd-id">${esc(agent.name)}</span>
        ${agent.display_name ? `<span class="sim-acrd-display">${esc(agent.display_name)}</span>` : ''}
      </div>
      <label class="sim-active-toggle" title="처음부터 등장">
        <input type="checkbox" class="acrd-active-cb" data-idx="${idx}" ${isActive ? 'checked' : ''}/>
        <span class="track"></span><span class="thumb"></span>
      </label>
      <span class="sim-acrd-active-label${isActive ? ' active' : ''}">${isActive ? '초기' : '대기'}</span>
      <span class="sim-acrd-arrow">${isExpanded ? '▲' : '▼'}</span>
      <button class="sim-acrd-del" data-idx="${idx}" title="삭제">🗑</button>
    `;

    // ── Body ──
    const gender    = agent.gender || 'auto';
    const autoIcon  = getAgentIcon(agent, 'neutral');
    const body = document.createElement('div');
    body.className = `sim-acrd-body${isExpanded ? '' : ' sim-acrd-collapsed'}`;
    body.innerHTML = `
      <div class="sim-acrd-field-row">
        <div class="sim-acrd-field">
          <label>성별 / 아이콘</label>
          <div class="sim-gender-row">
            <span class="sim-icon-preview" id="sim-icon-prev-${idx}">${esc(autoIcon)}</span>
            <div class="sim-gender-btns">
              <button class="sim-gender-btn${gender === 'auto'   ? ' active' : ''}" data-idx="${idx}" data-gender="auto">자동</button>
              <button class="sim-gender-btn${gender === 'male'   ? ' active' : ''}" data-idx="${idx}" data-gender="male">👨 남</button>
              <button class="sim-gender-btn${gender === 'female' ? ' active' : ''}" data-idx="${idx}" data-gender="female">👩 여</button>
            </div>
            <input class="sim-acrd-input-icon" data-idx="${idx}" data-field="icon"
                   value="${agent.icon !== '🤖' ? esc(agent.icon) : ''}"
                   maxlength="4" placeholder="직접 입력"/>
          </div>
        </div>
        <div class="sim-acrd-field">
          <label>ID</label>
          <input class="sim-acrd-input-id" data-idx="${idx}" data-field="name"
                 value="${esc(agent.name)}" placeholder="영문 ID"/>
        </div>
        <div class="sim-acrd-field">
          <label>표시이름</label>
          <input class="sim-acrd-input-display" data-idx="${idx}" data-field="display_name"
                 value="${esc(agent.display_name || '')}" placeholder="한국어 이름 (선택)"/>
        </div>
      </div>
      <div class="agent-groups-row">
        <label>그룹</label>
        <div class="agent-groups-chips" data-agent-idx="${idx}">
          ${(agent.groups || []).map(g => `
            <span class="group-chip">
              ${esc(g)}
              <button class="group-chip-remove" data-agent-idx="${idx}" data-group="${esc(g)}" title="그룹 제거">×</button>
            </span>
          `).join('')}
          <input class="group-chip-input" type="text"
                 placeholder="그룹 추가..."
                 data-agent-idx="${idx}"
                 list="sim-groups-datalist-${idx}">
          <datalist id="sim-groups-datalist-${idx}">
            ${getAllGroups().map(g => `<option value="${esc(g)}">`).join('')}
          </datalist>
        </div>
      </div>
      <div class="sim-acrd-field-row">
        <div class="sim-acrd-field" style="flex:1">
          <label>위치</label>
          <input class="sim-acrd-input" data-idx="${idx}" data-field="location"
                 value="${esc(agent.location || '')}" placeholder="위치 (예: 화산파)"/>
        </div>
      </div>
      <div class="sim-acrd-prompt-row">
        <label>외모 묘사 <span style="font-weight:400;color:#94a3b8">(모르는 사람에게 보이는 외모)</span></label>
        <textarea class="sim-acrd-prompt" data-idx="${idx}" data-field="visual_description"
                  rows="2" placeholder="키가 크고 검은 도복을 입은 청년...">${esc(agent.visual_description || '')}</textarea>
      </div>
      <div class="sim-acrd-prompt-row">
        <label>시스템 프롬프트</label>
        <textarea class="sim-acrd-prompt" data-idx="${idx}" data-field="system_prompt"
                  placeholder="에이전트의 성격과 역할을 입력하세요...">${esc(agent.system_prompt)}</textarea>
      </div>
    `;

    row.appendChild(header);
    row.appendChild(body);
    list.appendChild(row);

    // Header area click → toggle expand (skip buttons/inputs/labels)
    header.addEventListener('click', e => {
      if (e.target.closest('button') || e.target.closest('input') || e.target.closest('label')) return;
      _toggleExpand(agent.name, body, header.querySelector('.sim-acrd-arrow'));
    });
    header.querySelector('.sim-acrd-arrow').addEventListener('click', () => {
      _toggleExpand(agent.name, body, header.querySelector('.sim-acrd-arrow'));
    });

    // Active toggle
    header.querySelector('.acrd-active-cb').addEventListener('change', e => {
      sim.agents[idx].initial_active = e.target.checked;
      const lbl = header.querySelector('.sim-acrd-active-label');
      lbl.textContent = e.target.checked ? '초기' : '대기';
      lbl.classList.toggle('active', e.target.checked);
      renderScenarioEvents();
    });

    // Delete
    header.querySelector('.sim-acrd-del').addEventListener('click', () => {
      _expandedAgents.delete(agent.name);
      sim.agents.splice(idx, 1);
      renderAgentListInConfig();
      renderStartAgentSelect();
    });

    // Group chip — Enter or comma adds a group
    body.querySelector('.group-chip-input')?.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ',') return;
      e.preventDefault();
      const val = e.target.value.trim().replace(/,/g, '');
      if (!val) return;
      const i = +e.target.dataset.agentIdx;
      if (!sim.agents[i].groups) sim.agents[i].groups = [];
      if (!sim.agents[i].groups.includes(val)) sim.agents[i].groups.push(val);
      e.target.value = '';
      renderAgentListInConfig();
    });

    // Group chip — × button removes a group
    body.querySelectorAll('.group-chip-remove').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const i = +btn.dataset.agentIdx;
        const g = btn.dataset.group;
        sim.agents[i].groups = (sim.agents[i].groups || []).filter(x => x !== g);
        renderAgentListInConfig();
      });
    });

    // Gender buttons
    body.querySelectorAll('.sim-gender-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const i = +btn.dataset.idx;
        sim.agents[i].gender = btn.dataset.gender;
        body.querySelectorAll('.sim-gender-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        _updateIconPreview(i, body, header);
      });
    });

    // Body field inputs — live-update sim state and header display
    body.querySelectorAll('[data-field]').forEach(el => {
      el.addEventListener('input', () => {
        const i = +el.dataset.idx;
        const oldName = sim.agents[i].name;
        sim.agents[i][el.dataset.field] = el.value;

        if (el.dataset.field === 'name') {
          if (_expandedAgents.has(oldName)) {
            _expandedAgents.delete(oldName);
            _expandedAgents.add(el.value);
          }
          header.querySelector('.sim-acrd-id').textContent = el.value;
          renderStartAgentSelect();
          renderScenarioEvents();

        } else if (el.dataset.field === 'icon') {
          // icon override: if empty, revert to '🤖' (triggers auto)
          if (!el.value.trim()) sim.agents[i].icon = '🤖';
          _updateIconPreview(i, body, header);

        } else if (el.dataset.field === 'display_name') {
          const namesEl = header.querySelector('.sim-acrd-names');
          let displayEl = namesEl.querySelector('.sim-acrd-display');
          if (el.value.trim()) {
            if (!displayEl) {
              displayEl = document.createElement('span');
              displayEl.className = 'sim-acrd-display';
              namesEl.appendChild(displayEl);
            }
            displayEl.textContent = el.value;
          } else {
            displayEl?.remove();
          }
          _updateIconPreview(i, body, header);

        } else if (el.dataset.field === 'system_prompt') {
          _updateIconPreview(i, body, header);
        }
      });
    });
  });
}

function _toggleExpand(agentName, bodyEl, arrowEl) {
  if (_expandedAgents.has(agentName)) {
    _expandedAgents.delete(agentName);
    bodyEl.classList.add('sim-acrd-collapsed');
    arrowEl.textContent = '▼';
  } else {
    _expandedAgents.add(agentName);
    bodyEl.classList.remove('sim-acrd-collapsed');
    arrowEl.textContent = '▲';
  }
}

function _updateIconPreview(idx, bodyEl, headerEl) {
  const agent   = sim.agents[idx];
  const icon    = getAgentIcon(agent, 'neutral');
  const prevEl  = bodyEl.querySelector(`#sim-icon-prev-${idx}`);
  if (prevEl) prevEl.textContent = icon;
  headerEl.querySelector('.sim-acrd-icon').textContent = icon;
}
