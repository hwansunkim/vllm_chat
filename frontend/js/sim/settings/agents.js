// frontend/js/sim/settings/agents.js
// Accordion editor for the agents list (settings view).

import { sim, esc, _expandedAgents, getAllGroups, detectGender, getAgentIcon,
         normalizeTemperature, normalizeAgentTemperature } from '../state.js';
import { renderScenarioEvents } from './events.js';
import { renderPatientZeroPicker } from './infection-config.js';
import { getServerList, peekServerList } from './server-list.js';
import { updateSectionBadges } from './sections.js';
import { autoGrowAll } from './textareas.js';

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
      <span class="sim-acrd-arrow${isExpanded ? ' expanded' : ''}" aria-hidden="true">▾</span>
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
        <div class="sim-acrd-field">
          <label>LLM 서버</label>
          <select class="sim-acrd-server-select" data-idx="${idx}" data-field="server_id"
                  title="이 에이전트의 턴/언어교정/메모리압축에만 적용됩니다">
            <option value="">기본 서버 사용</option>
          </select>
        </div>
        <div class="sim-acrd-field">
          <label>온도</label>
          <input type="number" class="sim-acrd-input-temp" data-idx="${idx}" data-field="temperature"
                 min="0" max="2" step="0.1"
                 value="${normalizeAgentTemperature(agent.temperature) ?? ''}"
                 placeholder="${_tempPlaceholder()}"
                 title="비워두면 시뮬레이션 기본 temperature를 사용합니다"/>
        </div>
      </div>
      <div class="sim-acrd-prompt-row">
        <label>외모 묘사 <span style="font-weight:400;color:#94a3b8">(모르는 사람에게 보이는 외모)</span></label>
        <textarea class="sim-acrd-prompt" data-idx="${idx}" data-field="visual_description" data-autogrow
                  placeholder="키가 크고 검은 도복을 입은 청년...">${esc(agent.visual_description || '')}</textarea>
      </div>
      <div class="sim-acrd-prompt-row">
        <label>시스템 프롬프트</label>
        <div class="sim-ta-wrap sim-acrd-prompt-wrap">
          <textarea class="sim-acrd-prompt" data-idx="${idx}" data-field="system_prompt" data-autogrow
                    placeholder="에이전트의 성격과 역할을 입력하세요...">${esc(agent.system_prompt)}</textarea>
          <button type="button" class="sim-zoom-btn" title="확대 편집">⤢</button>
        </div>
      </div>
    `;

    row.appendChild(header);
    row.appendChild(body);
    list.appendChild(row);

    // 확대 오버레이 제목 — 따옴표가 섞인 이름도 안전하도록 속성이 아니라 프로퍼티로 넣는다.
    const promptWrap = body.querySelector('.sim-acrd-prompt-wrap');
    if (promptWrap) {
      promptWrap.dataset.zoomTitle = `👤 ${agent.display_name || agent.name} — 시스템 프롬프트`;
    }

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
      // 삭제된 사람을 가리키던 감염 시드를 여기서 정리한다(피커가 prune한다).
      renderPatientZeroPicker();
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
    // <select>는 'input'도 발화하지만 의미가 분명한 'change'로 듣는다.
    body.querySelectorAll('[data-field]').forEach(el => {
      const evtName = el.tagName === 'SELECT' ? 'change' : 'input';
      el.addEventListener(evtName, () => {
        const i = +el.dataset.idx;
        const oldName = sim.agents[i].name;
        const field   = el.dataset.field;
        // server_id / temperature 는 "" (= 시뮬레이션 기본값 사용) 을 null로 정규화해서 저장한다.
        if (field === 'server_id') {
          sim.agents[i][field] = el.value || null;
        } else if (field === 'temperature') {
          sim.agents[i][field] = normalizeAgentTemperature(el.value);
        } else {
          sim.agents[i][field] = el.value;
        }

        if (el.dataset.field === 'name') {
          if (_expandedAgents.has(oldName)) {
            _expandedAgents.delete(oldName);
            _expandedAgents.add(el.value);
          }
          header.querySelector('.sim-acrd-id').textContent = el.value;
          // 감염 시드는 이름으로 에이전트를 가리키므로 rename을 따라가야 한다.
          // 먼저 고치지 않으면 renderScenarioEvents()의 _syncAgentSelection()이
          // "없는 에이전트"로 보고 첫 에이전트에게 조용히 환자 0번을 옮겨 붙인다.
          sim.events?.forEach(ev => {
            if (ev?.type === 'infect_agent' && ev.agent === oldName) ev.agent = el.value;
          });
          renderStartAgentSelect();
          renderScenarioEvents();
          renderPatientZeroPicker();

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

    // 온도 입력 확정(blur/Enter) 시 정규화된 값을 입력란에 되써서, 화면에 남은 범위 밖
    // 값(예: 3)과 상태에 저장된 클램프 값(2)이 어긋나 보이지 않게 한다.
    body.querySelector('.sim-acrd-input-temp')?.addEventListener('change', e => {
      const i = +e.target.dataset.idx;
      if (!sim.agents[i]) return;
      const norm = normalizeAgentTemperature(e.target.value);
      sim.agents[i].temperature = norm;
      e.target.value = norm ?? '';
    });
  });

  // 에이전트별 LLM 서버 드롭다운 채우기.
  // 캐시가 이미 있으면 동기적으로 채워 재렌더링 시 깜빡임을 막고,
  // 없을 때만 fetch를 기다린다 (요청은 카드 수와 무관하게 1회).
  const cached = peekServerList();
  if (cached) _fillAgentServerSelects(cached);
  else getServerList().then(_fillAgentServerSelects);

  // 카드 추가/삭제로 개수가 바뀌면 섹션 헤더의 요약 뱃지도 따라와야 한다.
  updateSectionBadges(sim);
  autoGrowAll(list);
}

// 에이전트 온도 입력이 비었을 때 보여줄 안내문 — 상속하는 시뮬레이션 기본값을 노출한다.
function _tempPlaceholder() {
  return `기본 ${normalizeTemperature(sim.temperature).toFixed(1)}`;
}

/**
 * 시뮬레이션 기본 temperature가 바뀌었을 때, 카드들의 온도 placeholder만 갱신한다.
 * 전체 재렌더링은 펼침/포커스 상태를 잃게 하므로 슬라이더 드래그 중에는 쓰지 않는다.
 */
export function refreshAgentTempPlaceholders() {
  const ph = _tempPlaceholder();
  document.getElementById('sim-agent-list')
    ?.querySelectorAll('.sim-acrd-input-temp')
    .forEach(el => { el.placeholder = ph; });
}

// 현재 DOM에 있는 모든 에이전트 서버 드롭다운에 같은 서버 목록을 채운다.
function _fillAgentServerSelects(servers) {
  const list = document.getElementById('sim-agent-list');
  if (!list) return;

  list.querySelectorAll('select[data-field="server_id"]').forEach(sel => {
    const idx   = +sel.dataset.idx;
    const agent = sim.agents[idx];
    // fetch 도중 에이전트가 삭제됐을 수 있다.
    if (!agent) return;

    const current = agent.server_id ?? '';
    // 비활성 서버는 선택지에서 뺀다 — 골라도 registry가 그 provider를 안 들고
    // 있어서 결국 조용히 다른(전역 기본) 서버로 새기 때문에, 애초에 고를 수
    // 없게 막는 편이 안전하다.
    const selectable = servers.filter(s => s.enabled !== false);

    sel.innerHTML = '';
    const defOpt = document.createElement('option');
    defOpt.value = '';
    defOpt.textContent = '기본 서버 사용';
    sel.appendChild(defOpt);

    selectable.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = s.name;
      sel.appendChild(opt);
    });

    // 저장된 server_id가 목록에 없는 경우(서버 삭제/비활성 등): 값을 조용히 버리지
    // 않고 경고 옵션으로 남겨 사용자가 알아채도록 한다. 백엔드는 이런 id를 만나면
    // 에러 없이 기본 서버로 폴백한다.
    if (current && !selectable.some(s => s.id === current)) {
      const opt = document.createElement('option');
      opt.value = current;
      opt.textContent = `⚠ 알 수 없는 서버 (${current.slice(0, 8)}…)`;
      sel.appendChild(opt);
    }

    sel.value = current;
  });
}

function _toggleExpand(agentName, bodyEl, arrowEl) {
  if (_expandedAgents.has(agentName)) {
    _expandedAgents.delete(agentName);
    bodyEl.classList.add('sim-acrd-collapsed');
    arrowEl.classList.remove('expanded');
  } else {
    _expandedAgents.add(agentName);
    bodyEl.classList.remove('sim-acrd-collapsed');
    arrowEl.classList.add('expanded');
    // 카드는 display:none 으로 접히므로 접혀 있는 동안 scrollHeight 가 0 이다.
    // 펼친 직후 다시 재보지 않으면 프롬프트 textarea 가 전부 MIN 으로 찌부러진다.
    autoGrowAll(bodyEl);
  }
}

function _updateIconPreview(idx, bodyEl, headerEl) {
  const agent   = sim.agents[idx];
  const icon    = getAgentIcon(agent, 'neutral');
  const prevEl  = bodyEl.querySelector(`#sim-icon-prev-${idx}`);
  if (prevEl) prevEl.textContent = icon;
  headerEl.querySelector('.sim-acrd-icon').textContent = icon;
}
