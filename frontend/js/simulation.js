import { esc } from './utils.js';

// ── Module state ──────────────────────────────────────────────────────────────
const sim = {
  status:              'idle',
  selectedAgent:       null,
  currentScenarioId:   null,
  currentScenarioName: '',
  agents:       [],
  background:   '',
  start_agent:  '',
  max_waves:    10,
  step_delay:   1.0,
  token_limit:  8192,
  extra_fields: [
    { name: 'emotion', default: 'neutral' },
    { name: 'action',  default: 'speak'   },
  ],
  events:       [],
  output_format_template: '',
  eventSource:  null,
  scenarios:    [],
};

// Accordion expand state (keyed by agent.name)
const _expandedAgents = new Set();

// ── Emotion helpers ───────────────────────────────────────────────────────────
const EMOTION_COLORS = {
  angry: '#ef4444', happy: '#22c55e', neutral: '#94a3b8',
  sad: '#3b82f6', fear: '#f97316',
};
const EMOTION_CLASS = ['angry','happy','neutral','sad','fear'];

function emotionColor(e) { return EMOTION_COLORS[e] || '#a78bfa'; }
function emotionClass(e) { return EMOTION_CLASS.includes(e) ? `emotion-${e}` : 'emotion-neutral'; }

// ── View management ───────────────────────────────────────────────────────────
function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  _updateScenarioLabel();
  renderAgentCards();
  initD3Graph();
  loadScenarios();
}

function hideSimView() {
  document.getElementById('sim-view').classList.add('sim-hidden');
  document.getElementById('main').classList.remove('sim-hidden');
}

function showSettingsView() {
  document.getElementById('sim-view').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.remove('sim-hidden');
  renderSettingsPage();
  loadScenarios();
}

function hideSettingsView() {
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  _updateScenarioLabel();
  renderAgentCards();
}

function _updateScenarioLabel() {
  const el = document.getElementById('sim-scenario-label');
  if (el) el.textContent = sim.currentScenarioName || '시나리오 없음';
}

// ── Settings page ─────────────────────────────────────────────────────────────
function renderSettingsPage() {
  document.getElementById('sim-scenario-name').value  = sim.currentScenarioName;
  document.getElementById('sim-background').value     = sim.background;
  document.getElementById('sim-max-waves').value      = sim.max_waves;
  document.getElementById('sim-step-delay').value     = sim.step_delay;
  document.getElementById('sim-token-limit').value    = sim.token_limit;
  document.getElementById('sim-output-format').value  = sim.output_format_template || '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = !sim.currentScenarioId;
  renderOutputFields();
  renderAgentListInConfig();
  renderStartAgentSelect();
  renderScenarioEvents();
}

// ── Output Fields ─────────────────────────────────────────────────────────────
function renderOutputFields() {
  const list = document.getElementById('sim-fields-list');
  if (!list) return;
  list.innerHTML = '';

  if (!sim.extra_fields.length) {
    list.innerHTML = '<div class="sim-fields-empty">메타데이터 필드 없음 — content, target만 사용됩니다.</div>';
    return;
  }

  sim.extra_fields.forEach((f, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-field-row';
    row.innerHTML = `
      <input class="sim-field-name" type="text" placeholder="필드명 (영문)"
             value="${esc(f.name)}" data-idx="${idx}" data-prop="name"/>
      <span class="sim-field-sep">:</span>
      <input class="sim-field-default" type="text" placeholder="기본값"
             value="${esc(f.default)}" data-idx="${idx}" data-prop="default"/>
      <button class="sim-field-del" data-idx="${idx}">✕</button>
    `;
    list.appendChild(row);

    row.querySelectorAll('[data-prop]').forEach(el => {
      el.addEventListener('input', () => {
        sim.extra_fields[+el.dataset.idx][el.dataset.prop] = el.value;
      });
    });
    row.querySelector('.sim-field-del').addEventListener('click', () => {
      sim.extra_fields.splice(idx, 1);
      renderOutputFields();
    });
  });
}

// ── Agent list (accordion) ────────────────────────────────────────────────────
function renderStartAgentSelect() {
  const sel  = document.getElementById('sim-start-agent');
  const prev = sel.value;
  sel.innerHTML = '';
  sim.agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = `${a.icon} ${a.name}`;
    sel.appendChild(opt);
  });
  if (prev && sim.agents.find(a => a.name === prev)) sel.value = prev;
  else if (sim.agents.length) sel.value = sim.agents[0].name;
  sim.start_agent = sel.value;
}

function renderAgentListInConfig() {
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
      <span class="sim-acrd-icon">${esc(agent.icon)}</span>
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
    const body = document.createElement('div');
    body.className = `sim-acrd-body${isExpanded ? '' : ' sim-acrd-collapsed'}`;
    body.innerHTML = `
      <div class="sim-acrd-field-row">
        <div class="sim-acrd-field">
          <label>아이콘</label>
          <input class="sim-acrd-input-icon" data-idx="${idx}" data-field="icon"
                 value="${esc(agent.icon)}" maxlength="4"/>
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
          header.querySelector('.sim-acrd-icon').textContent = el.value;

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

function readConfigFromUI() {
  sim.background             = document.getElementById('sim-background').value.trim();
  sim.start_agent            = document.getElementById('sim-start-agent').value;
  sim.max_waves              = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.step_delay             = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)  || 8192;
  sim.output_format_template = document.getElementById('sim-output-format').value;
}

// ── Agent Cards ───────────────────────────────────────────────────────────────
function renderAgentCards() {
  const container = document.getElementById('sim-agent-cards');
  container.innerHTML = '';
  sim.agents.forEach(agent => {
    const card = document.createElement('div');
    const inactive = agent.initial_active === false;
    card.className = `sim-agent-card${inactive ? ' inactive' : ''}`;
    card.id = `simc-${agent.name}`;
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

    card.innerHTML = `
      <div class="sim-card-header">
        <span class="sim-card-icon">${esc(agent.icon)}</span>
        <span class="sim-card-name">${displayLabel}</span>
      </div>
      <div class="sim-card-meta">${metaHtml}</div>
      <div class="sim-card-token-row">
        <div class="sim-card-token-bar-wrap">
          <div class="sim-card-token-bar-fill" id="simc-tok-${esc(agent.name)}" style="width:0%"></div>
        </div>
        <span class="sim-card-token-label" id="simc-tokl-${esc(agent.name)}">— / ${_fmtK(sim.token_limit)}</span>
      </div>
      <div class="sim-card-preview" id="simc-pre-${esc(agent.name)}">대기 중...</div>
    `;
    card.addEventListener('click', () => openAgentContext(agent.name));
    container.appendChild(card);
  });
}

function _fmtK(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function updateAgentCard(speaker, meta, promptTokens, tokenLimit, preview) {
  Object.entries(meta || {}).forEach(([field, value]) => {
    const el = document.getElementById(`simc-meta-${field}-${speaker}`);
    if (!el) return;
    el.textContent = value;
    if (field === 'emotion') {
      el.className = `sim-feed-badge ${emotionClass(String(value))}`;
    }
  });

  if (promptTokens && tokenLimit) {
    const pct = Math.min(100, (promptTokens / tokenLimit) * 100);
    const barEl  = document.getElementById(`simc-tok-${speaker}`);
    const lblEl  = document.getElementById(`simc-tokl-${speaker}`);
    if (barEl) {
      barEl.style.width = `${pct}%`;
      barEl.className   = `sim-card-token-bar-fill${pct >= 90 ? ' danger' : pct >= 70 ? ' warn' : ''}`;
    }
    if (lblEl) lblEl.textContent = `${_fmtK(promptTokens)} / ${_fmtK(tokenLimit)}`;
  }

  const preEl = document.getElementById(`simc-pre-${speaker}`);
  if (preEl && preview) preEl.textContent = preview.slice(0, 42);
}

// ── Feed ──────────────────────────────────────────────────────────────────────
function removeFeedEmpty() {
  const el = document.getElementById('sim-feed-empty');
  if (el) el.remove();
}

function addTypingIndicator(speaker) {
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

function removeTypingIndicator(speaker) {
  if (speaker) {
    document.getElementById(`sim-typing-${speaker}`)?.remove();
  } else {
    document.querySelectorAll('[id^="sim-typing-"]').forEach(el => el.remove());
  }
}

function addFeedMessage(data) {
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

// ── Wave indicator ─────────────────────────────────────────────────────────────
function updateWaveIndicator(waveNum, agents) {
  document.getElementById('sim-turn-text').textContent =
    `Wave ${waveNum}  |  ${agents.join(', ')}`;
  document.getElementById('sim-progress-fill').style.width = '30%';
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  if (sim.eventSource) { sim.eventSource.close(); sim.eventSource = null; }

  const es = new EventSource('/api/simulation/stream');
  sim.eventSource = es;

  es.addEventListener('wave_start', e => {
    const d = JSON.parse(e.data);
    removeTypingIndicator();
    document.querySelectorAll('.sim-agent-card').forEach(c => c.classList.remove('speaking'));
    d.agents.forEach(name => {
      document.getElementById(`simc-${name}`)?.classList.add('speaking');
    });
    updateWaveIndicator(d.wave, d.agents);
  });

  es.addEventListener('turn_start', e => {
    const d = JSON.parse(e.data);
    addTypingIndicator(d.speaker);
  });

  es.addEventListener('turn_complete', e => {
    const d = JSON.parse(e.data);
    addFeedMessage(d);
    updateAgentCard(d.speaker, d.meta || {}, d.prompt_tokens, d.token_limit, d.content);
    d.new_edges?.forEach(edge => addD3Edge(edge.source, edge.target, edge.emotion || (edge.meta || {}).emotion || 'neutral'));
    document.getElementById(`simc-${d.speaker}`)?.classList.remove('speaking');
    if (sim.selectedAgent === d.speaker && !document.getElementById('sim-tab-context').classList.contains('sim-hidden')) {
      fetchAgentContext(d.speaker);
    }
  });

  es.addEventListener('turn_error', e => {
    const d = JSON.parse(e.data);
    removeTypingIndicator(d.speaker);
    document.getElementById(`simc-${d.speaker}`)?.classList.remove('speaking');
  });

  es.addEventListener('scene_event', e => {
    const d = JSON.parse(e.data);
    addSceneEventToFeed(d);
    if (d.event_type === 'agent_enter') {
      document.getElementById(`simc-${d.agent}`)?.classList.remove('inactive');
    } else if (d.event_type === 'agent_exit') {
      const card = document.getElementById(`simc-${d.agent}`);
      if (card) { card.classList.remove('speaking'); card.classList.add('exited'); }
    }
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

// ── Simulation control ────────────────────────────────────────────────────────
async function startSimulation() {
  readConfigFromUI();

  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.length) { alert('에이전트를 하나 이상 추가하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

  document.getElementById('sim-feed').innerHTML =
    '<div id="sim-feed-empty">시뮬레이션 시작 중...</div>';
  document.getElementById('sim-turn-text').textContent = '대기 중';
  document.getElementById('sim-progress-fill').style.width = '0%';
  renderAgentCards();
  initD3Graph();

  const res = await fetch('/api/simulation/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenario_id:            sim.currentScenarioId || null,
      agents:                 sim.agents,
      background:             sim.background,
      start_agent:            sim.start_agent,
      max_waves:              sim.max_waves,
      step_delay:             sim.step_delay,
      token_limit:            sim.token_limit,
      extra_fields:           sim.extra_fields,
      events:                 sim.events,
      output_format_template: sim.output_format_template || '',
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`시작 실패: ${err.detail || '서버 오류'}`);
    return;
  }

  setStatus('running');
  connectSSE();
}

async function stopSimulation() {
  await fetch('/api/simulation/stop', { method: 'POST' });
  setStatus('stopped');
  removeTypingIndicator();
  if (sim.eventSource) { sim.eventSource.close(); sim.eventSource = null; }
}

function setStatus(status) {
  sim.status = status;
  const badge  = document.getElementById('sim-status-badge');
  const labels = { idle: '대기 중', running: '실행 중', done: '완료', stopped: '중지됨', error: '오류' };
  badge.textContent = labels[status] || status;
  badge.className   = `sim-status-badge ${status}`;
  document.getElementById('sim-start-btn').disabled    = status === 'running';
  document.getElementById('sim-continue-btn').disabled = !['done', 'stopped'].includes(status);
  document.getElementById('sim-stop-btn').disabled     = status !== 'running';
}

async function continueSimulation() {
  readConfigFromUI();
  if (!sim.start_agent) { alert('시작 에이전트를 선택하세요.'); return; }
  if (!sim.agents.find(a => a.name === sim.start_agent)) {
    alert(`시작 에이전트 '${sim.start_agent}'가 에이전트 목록에 없습니다.`); return;
  }

  const res = await fetch('/api/simulation/continue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      start_agent: sim.start_agent,
      max_waves:   sim.max_waves,
      step_delay:  sim.step_delay,
      events:      sim.events,
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(`이어서 실행 실패: ${err.detail || '서버 오류'}`);
    return;
  }

  setStatus('running');
  connectSSE();
}

// ── Scenarios ─────────────────────────────────────────────────────────────────
async function loadScenarios() {
  const res = await fetch('/api/simulation/scenarios');
  sim.scenarios = await res.json();
  const sel = document.getElementById('sim-scenario-select');
  sel.innerHTML = '<option value="">-- 불러오기 --</option>';
  sim.scenarios.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    sel.appendChild(opt);
  });
  // 현재 시나리오가 로드된 상태면 이력 버튼 활성화
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) histBtn.disabled = !sim.currentScenarioId;
}

async function saveScenario() {
  readConfigFromUI();
  const nameEl = document.getElementById('sim-scenario-name');
  const name   = nameEl.value.trim();
  if (!name) { nameEl.focus(); nameEl.style.borderColor = '#ef4444'; return; }
  nameEl.style.borderColor = '';

  const payload = {
    name,
    description: '',
    config: {
      agents:                 sim.agents,
      background:             sim.background,
      start_agent:            sim.start_agent,
      max_waves:              sim.max_waves,
      step_delay:             sim.step_delay,
      token_limit:            sim.token_limit,
      extra_fields:           sim.extra_fields,
      events:                 sim.events,
      output_format_template: sim.output_format_template || '',
    },
  };

  let res;
  if (sim.currentScenarioId) {
    res = await fetch(`/api/simulation/scenarios/${sim.currentScenarioId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } else {
    res = await fetch('/api/simulation/scenarios', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      const data = await res.json();
      sim.currentScenarioId = data.id;
    }
  }

  if (res.ok) {
    sim.currentScenarioName = name;
    const delBtn = document.getElementById('sim-delete-scenario-btn');
    if (delBtn) delBtn.disabled = false;
    await loadScenarios();
    const btn = document.getElementById('sim-save-scenario-btn');
    const orig = btn.textContent;
    btn.textContent = '✓ 저장됨';
    btn.style.color = '#15803d';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
  }
}

async function deleteScenario() {
  if (!sim.currentScenarioId) return;
  if (!confirm(`시나리오 "${sim.currentScenarioName}"을(를) 삭제하시겠습니까?`)) return;
  await fetch(`/api/simulation/scenarios/${sim.currentScenarioId}`, { method: 'DELETE' });
  sim.currentScenarioId   = null;
  sim.currentScenarioName = '';
  document.getElementById('sim-scenario-name').value = '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = true;
  await loadScenarios();
}

// ── Run History ───────────────────────────────────────────────────────────────
async function toggleRunHistory() {
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel) return;
  const isVisible = !panel.classList.contains('sim-hidden');
  if (isVisible) {
    panel.classList.add('sim-hidden');
    return;
  }
  if (!sim.currentScenarioId) return;
  panel.classList.remove('sim-hidden');
  await refreshRunHistory();
}

async function refreshRunHistory() {
  if (!sim.currentScenarioId) return;
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel || panel.classList.contains('sim-hidden')) return;

  const res  = await fetch(`/api/simulation/runs?scenario_id=${encodeURIComponent(sim.currentScenarioId)}`);
  const runs = await res.json();

  const statusIcon = { running: '🔄', done: '✅', stopped: '⏹', error: '❌' };
  const fmtTime = ts => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} `
         + `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  };

  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) histBtn.textContent = `📋 이력 (${runs.length})`;

  if (!runs.length) {
    panel.innerHTML = '<div class="sim-run-history-empty">아직 실행 이력이 없습니다.</div>';
    return;
  }

  const rows = runs.map(r => `
    <tr>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at)}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td class="rh-view">
        <button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁 보기</button>
      </td>
      <td class="rh-del">
        <button class="sim-run-del-btn" data-run-id="${esc(r.run_id)}">🗑</button>
      </td>
    </tr>`).join('');

  panel.innerHTML = `
    <table class="sim-run-history-table">
      <thead><tr>
        <th>#</th><th>시작</th><th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  panel.querySelectorAll('.sim-run-view-btn').forEach(btn => {
    btn.addEventListener('click', () => openRunReplay(btn.dataset.runId, btn.dataset.runNum));
  });

  panel.querySelectorAll('.sim-run-del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('이 실행 이력과 메모리 데이터를 삭제하시겠습니까?')) return;
      await fetch(`/api/simulation/runs/${btn.dataset.runId}`, { method: 'DELETE' });
      await refreshRunHistory();
    });
  });
}

async function openAllRunsModal() {
  document.getElementById('sim-all-runs-modal')?.remove();

  const res = await fetch('/api/simulation/runs');
  if (!res.ok) { alert('이력을 불러오지 못했습니다.'); return; }
  const runs = await res.json();

  const statusIcon = { running: '🔄', done: '✅', stopped: '⏹', error: '❌' };
  const fmtTime = ts => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} `
         + `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  };
  const canResume = r => { try { const c = JSON.parse(r.config_json||'{}'); return !!(c.agents?.length); } catch(_){return false;} };

  const modal = document.createElement('div');
  modal.id = 'sim-all-runs-modal';
  modal.className = 'sim-replay-modal-overlay';

  const rows = runs.length ? runs.map(r => `
    <tr>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at)}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${esc(r.scenario_name || '—')}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td><button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁 보기</button></td>
      <td><button class="sim-run-del-btn all-modal-del" data-run-id="${esc(r.run_id)}">🗑</button></td>
    </tr>`).join('') :
    '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:24px">실행 이력이 없습니다</td></tr>';

  modal.innerHTML = `
    <div class="sim-replay-modal-box" style="width:min(900px,96vw)">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span style="font-size:15px">📋</span> 전체 실행 이력
          <span class="sim-replay-meta">${runs.length}건</span>
        </div>
        <button id="sim-all-runs-close-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">✕ 닫기</button>
      </div>
      <div style="flex:1;overflow-y:auto;padding:12px 16px">
        <table class="sim-run-history-table" style="width:100%">
          <thead><tr>
            <th>#</th><th>시작</th><th>시나리오</th><th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;

  document.body.appendChild(modal);

  document.getElementById('sim-all-runs-close-btn').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  modal.querySelectorAll('.sim-run-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      modal.remove();
      openRunReplay(btn.dataset.runId, btn.dataset.runNum);
    });
  });

  modal.querySelectorAll('.all-modal-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('이 실행 이력과 메모리 데이터를 삭제하시겠습니까?')) return;
      await fetch(`/api/simulation/runs/${btn.dataset.runId}`, { method: 'DELETE' });
      modal.remove();
      openAllRunsModal();
    });
  });
}

async function openRunReplay(runId, runNum) {
  // 기존 모달 제거
  document.getElementById('sim-replay-modal')?.remove();

  const [runRes, logRes] = await Promise.all([
    fetch(`/api/simulation/runs/${encodeURIComponent(runId)}`),
    fetch(`/api/simulation/runs/${encodeURIComponent(runId)}/log`),
  ]);
  if (!runRes.ok || !logRes.ok) { alert('대화 기록을 불러오지 못했습니다.'); return; }

  const run = await runRes.json();
  const log = await logRes.json();

  const statusIcon = { running: '🔄', done: '✅', stopped: '⏹', error: '❌' };
  const fmtTime = ts => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} `
         + `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  };

  // config_json 파싱 (빈 객체면 재시작 불가)
  let parsedConfig = null;
  try {
    const c = JSON.parse(run.config_json || '{}');
    if (c.agents && c.agents.length) parsedConfig = c;
  } catch (_) {}

  const modal = document.createElement('div');
  modal.id = 'sim-replay-modal';
  modal.className = 'sim-replay-modal-overlay';
  modal.innerHTML = `
    <div class="sim-replay-modal-box">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span class="sim-replay-run-badge">#${runNum}</span>
          <span>${esc(run.scenario_name || '직접 실행')}</span>
          <span class="sim-replay-status">${statusIcon[run.status] || run.status}</span>
          <span class="sim-replay-meta">${fmtTime(run.started_at)} · ${run.total_waves}wave · ${run.total_turns}turn</span>
        </div>
        <div class="sim-replay-actions">
          ${parsedConfig ? `<button id="sim-replay-resume-btn" class="sim-ctrl-btn continue" style="font-size:12px;padding:4px 10px">↩ 이어서</button>` : ''}
          ${parsedConfig ? `<button id="sim-replay-restart-btn" class="sim-ctrl-btn start" style="font-size:12px;padding:4px 10px">▶ 새로 시작</button>` : ''}
          <button id="sim-replay-close-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">✕ 닫기</button>
        </div>
      </div>
      <div class="sim-replay-feed" id="sim-replay-feed">
        ${log.length === 0 ? '<div class="sim-feed-empty-msg">저장된 대화 기록이 없습니다.</div>' : ''}
      </div>
    </div>`;

  document.body.appendChild(modal);

  // 피드 렌더링
  const feedEl = document.getElementById('sim-replay-feed');
  log.forEach(entry => {
    const div = document.createElement('div');
    div.className = 'sim-feed-item';
    const agentObj = sim.agents.find(a => a.name === entry.speaker);
    const icon = agentObj?.icon || '🤖';
    const label = agentObj?.display_name || entry.speaker;
    const actionNote = entry.action_note || '';
    const extraFields = sim.extra_fields || [];
    const metaBadges = extraFields
      .filter(f => entry.meta && entry.meta[f.name] != null)
      .map(f => {
        const val = String(entry.meta[f.name]);
        const cls = f.name === 'emotion' ? emotionClass(val) : 'emotion-neutral';
        return `<span class="sim-feed-badge ${cls}">${esc(val)}</span>`;
      }).join('');
    const targets = Array.isArray(entry.targets) ? entry.targets.join(', ') : '';
    div.innerHTML = `
      <div class="sim-feed-speaker">
        <span class="sim-feed-icon">${esc(icon)}</span>
        <span class="sim-feed-name">${esc(label)}</span>
        <span class="sim-feed-wave-badge">W${entry.wave}</span>
      </div>
      <div class="sim-feed-body">
        <div class="sim-feed-content">${esc(entry.content)}</div>
        ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
        <div class="sim-feed-meta">
          ${metaBadges}
          ${targets ? `<span class="sim-feed-target">→ ${esc(targets)}</span>` : ''}
        </div>
      </div>`;
    feedEl.appendChild(div);
  });

  // 닫기
  document.getElementById('sim-replay-close-btn').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  // 이어서 실행 — 에이전트 메모리 복원 후 이어서
  const resumeBtn = document.getElementById('sim-replay-resume-btn');
  if (resumeBtn && parsedConfig) {
    resumeBtn.addEventListener('click', async () => {
      resumeBtn.disabled = true;
      resumeBtn.textContent = '복원 중...';
      const res = await fetch(`/api/simulation/resume/${encodeURIComponent(runId)}`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`재개 실패: ${err.detail || '서버 오류'}`);
        resumeBtn.disabled = false;
        resumeBtn.textContent = '↩ 이어서';
        return;
      }
      // 설정 반영 (에이전트 카드 등 UI 동기화)
      applyScenario({ id: run.scenario_id, name: run.scenario_name || '', config: parsedConfig });
      renderAgentCards();
      modal.remove();
      setStatus('running');
      connectSSE();
    });
  }

  // 새로 시작 — 같은 설정으로 처음부터
  const restartBtn = document.getElementById('sim-replay-restart-btn');
  if (restartBtn && parsedConfig) {
    restartBtn.addEventListener('click', () => {
      applyScenario({ id: run.scenario_id, name: run.scenario_name || '', config: parsedConfig });
      renderAgentCards();
      renderSettingsPage();
      modal.remove();
    });
  }
}

function newScenario() {
  sim.currentScenarioId   = null;
  sim.currentScenarioName = '';
  sim.agents              = [];
  sim.background          = '';
  sim.start_agent         = '';
  sim.max_waves           = 10;
  sim.step_delay          = 1.0;
  sim.token_limit         = 8192;
  sim.extra_fields        = [
    { name: 'emotion', default: 'neutral' },
    { name: 'action',  default: 'speak'   },
  ];
  sim.events                 = [];
  sim.output_format_template = '';
  _expandedAgents.clear();
  document.getElementById('sim-scenario-name').value = '';
  document.getElementById('sim-scenario-select').value = '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = true;
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) { histBtn.disabled = true; histBtn.textContent = '📋 이력'; }
  document.getElementById('sim-run-history-panel')?.classList.add('sim-hidden');
  renderSettingsPage();
}

function applyScenario(s) {
  const cfg = s.config;
  sim.currentScenarioId   = s.id;
  sim.currentScenarioName = s.name;
  sim.agents       = cfg.agents       || [];
  sim.background   = cfg.background   || '';
  sim.start_agent  = cfg.start_agent  || (cfg.agents?.[0]?.name ?? '');
  sim.max_waves    = cfg.max_waves    || 10;
  sim.step_delay   = cfg.step_delay   || 1.0;
  // Migrate legacy memory_limit (message count) to token_limit.
  // Old default was 20 messages; ~400 tokens/message is a reasonable estimate.
  sim.token_limit  = cfg.token_limit ?? (cfg.memory_limit ? cfg.memory_limit * 400 : 8192);
  sim.extra_fields = cfg.extra_fields || [
    { name: 'emotion', default: 'neutral' },
    { name: 'action',  default: 'speak'   },
  ];
  sim.events                 = cfg.events                 || [];
  sim.output_format_template = cfg.output_format_template || '';
  _expandedAgents.clear();
  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) {
    histBtn.disabled = false;
    histBtn.textContent = '📋 이력';
  }
  // 이력 패널이 열려있으면 갱신
  refreshRunHistory();
  renderSettingsPage();
}

// ── D3 Force Graph ─────────────────────────────────────────────────────────────
let _d3Sim  = null;
let _d3Data = { nodes: [], links: [], nodeMap: {} };

function initD3Graph() {
  _d3Data = { nodes: [], links: [], nodeMap: {} };
  const svgEl = document.getElementById('sim-graph-svg');
  const svg   = d3.select(svgEl);
  svg.selectAll('*').remove();

  const W = svgEl.clientWidth  || 280;
  const H = svgEl.clientHeight || 400;

  const defs = svg.append('defs');
  Object.entries(EMOTION_COLORS).forEach(([em, color]) => {
    defs.append('marker')
      .attr('id', `arr-${em}`)
      .attr('viewBox', '0 -4 8 8')
      .attr('refX', 24).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6)
      .attr('orient', 'auto')
      .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', color);
  });
  defs.append('marker').attr('id', 'arr-default')
    .attr('viewBox', '0 -4 8 8').attr('refX', 24).attr('refY', 0)
    .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
    .append('path').attr('d', 'M0,-4L8,0L0,4').attr('fill', '#a78bfa');

  const g = svg.append('g');

  svg.call(d3.zoom().scaleExtent([0.3, 4])
    .on('zoom', e => g.attr('transform', e.transform)));

  _d3Sim = d3.forceSimulation([])
    .force('link',      d3.forceLink([]).id(d => d.id).distance(110))
    .force('charge',    d3.forceManyBody().strength(-220))
    .force('center',    d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(36));

  _d3Sim.on('tick', () => {
    g.selectAll('.g-link').attr('d', linkPath);
    g.selectAll('.g-link-label')
      .attr('x', d => (d.source.x + d.target.x) / 2)
      .attr('y', d => (d.source.y + d.target.y) / 2 - 5);
    g.selectAll('.g-node').attr('transform', d => `translate(${d.x},${d.y})`);
  });

  svg.datum({ g, W, H });
}

function linkPath(d) {
  const dx = d.target.x - d.source.x;
  const dy = d.target.y - d.source.y;
  const dr = Math.sqrt(dx * dx + dy * dy) * 1.3;
  return `M${d.source.x},${d.source.y}A${dr},${dr} 0 0,1 ${d.target.x},${d.target.y}`;
}

function addD3Edge(source, target, emotion) {
  if (!_d3Sim) return;
  const svg  = d3.select('#sim-graph-svg');
  const gEl  = svg.datum()?.g;
  if (!gEl) return;

  [source, target].forEach(name => {
    if (name === 'system' || name === 'all' || _d3Data.nodeMap[name]) return;
    const agent = sim.agents.find(a => a.name === name) || { icon: '🤖', name };
    const node  = { id: name, icon: agent.icon };
    _d3Data.nodes.push(node);
    _d3Data.nodeMap[name] = node;
  });

  if (target !== 'system' && target !== 'all') {
    _d3Data.links.push({ source, target, emotion: emotion || 'neutral' });
  }

  _d3Sim.nodes(_d3Data.nodes);
  _d3Sim.force('link').links(_d3Data.links);

  const linkSel = gEl.selectAll('.g-link').data(_d3Data.links);
  linkSel.enter().append('path').attr('class', 'g-link')
    .merge(linkSel)
    .attr('stroke', d => emotionColor(d.emotion))
    .attr('marker-end', d => `url(#arr-${EMOTION_COLORS[d.emotion] ? d.emotion : 'default'})`);

  const lblSel = gEl.selectAll('.g-link-label').data(_d3Data.links);
  lblSel.enter().append('text').attr('class', 'g-link-label')
    .merge(lblSel).text(d => d.emotion);

  const nodeSel   = gEl.selectAll('.g-node').data(_d3Data.nodes, d => d.id);
  const nodeEnter = nodeSel.enter().append('g').attr('class', 'g-node')
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0); d.fx = null; d.fy = null; })
    );

  nodeEnter.append('circle').attr('r', 22).attr('fill', '#eef2ff');
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', -4)
    .attr('font-size', '17px').text(d => d.icon);
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', 14)
    .attr('font-size', '10px').attr('fill', '#475569').text(d => d.id);

  _d3Sim.alpha(0.4).restart();
}

// ── Export SVG ─────────────────────────────────────────────────────────────────
function exportGraph() {
  const svgEl  = document.getElementById('sim-graph-svg');
  const blob   = new Blob([new XMLSerializer().serializeToString(svgEl)], { type: 'image/svg+xml' });
  const url    = URL.createObjectURL(blob);
  const a      = Object.assign(document.createElement('a'), { href: url, download: 'sim-graph.svg' });
  a.click();
  URL.revokeObjectURL(url);
}

// ── Scenario events UI ────────────────────────────────────────────────────────
const EVENT_LABELS = {
  system_message: '📢 시스템 메시지',
  agent_enter:    '🎭 에이전트 등장',
  agent_exit:     '🚪 에이전트 퇴장',
};

function renderScenarioEvents() {
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

function addSceneEventToFeed(d) {
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

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tabName) {
  document.querySelectorAll('.sim-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.getElementById('sim-tab-graph').classList.toggle('sim-hidden', tabName !== 'graph');
  document.getElementById('sim-tab-context').classList.toggle('sim-hidden', tabName !== 'context');
  document.getElementById('sim-export-graph-btn').classList.toggle('sim-hidden', tabName !== 'graph');
  if (tabName === 'context' && sim.selectedAgent && sim.status !== 'idle') {
    fetchAgentContext(sim.selectedAgent);
  }
}

// ── Context window ────────────────────────────────────────────────────────────
async function openAgentContext(name) {
  sim.selectedAgent = name;
  switchTab('context');
  const a = sim.agents.find(ag => ag.name === name);
  const label = a
    ? `${a.icon} ${a.display_name || a.name}${a.display_name ? ` (${a.name})` : ''}`
    : `🤖 ${name}`;
  document.getElementById('sim-context-agent-name').textContent = label;
  document.getElementById('sim-context-refresh-btn').classList.remove('sim-hidden');
  await fetchAgentContext(name);
}

async function fetchAgentContext(name) {
  const msgs = document.getElementById('sim-context-msgs');
  msgs.innerHTML = '<div style="padding:12px;font-size:11px;color:#94a3b8;">로딩 중...</div>';
  try {
    const res  = await fetch(`/api/simulation/agents/${encodeURIComponent(name)}/context`);
    if (!res.ok) {
      msgs.innerHTML = `<div style="padding:12px;font-size:11px;color:#ef4444;">불러오기 실패 (${res.status})<br>시뮬레이션이 실행된 후 확인 가능합니다.</div>`;
      return;
    }
    const data = await res.json();
    renderContextMessages(data.messages, data.trimmed || 0, data.prompt_tokens || 0, data.token_limit || 0);
  } catch (e) {
    msgs.innerHTML = `<div style="padding:12px;font-size:11px;color:#ef4444;">오류: ${esc(String(e))}</div>`;
  }
}

function renderContextMessages(messages, trimmed = 0, promptTokens = 0, tokenLimit = 0) {
  const container = document.getElementById('sim-context-msgs');
  container.innerHTML = '';

  // Token usage bar
  if (tokenLimit > 0) {
    const pct = Math.min(100, (promptTokens / tokenLimit) * 100);
    const bar = document.createElement('div');
    bar.className = 'ctx-token-banner';
    bar.innerHTML = `
      <div class="ctx-token-info">
        <span class="ctx-token-used">${promptTokens.toLocaleString()}</span>
        <span class="ctx-token-sep">/</span>
        <span class="ctx-token-limit">${tokenLimit.toLocaleString()} 토큰</span>
        <span class="ctx-token-pct ${pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : ''}">(${pct.toFixed(1)}%)</span>
        ${trimmed > 0 ? `<span class="ctx-trim-badge">⚠ ${trimmed}개 제거됨</span>` : ''}
      </div>
      <div class="ctx-token-bar-wrap">
        <div class="ctx-token-bar-fill ${pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : ''}"
             style="width:${pct}%"></div>
      </div>
    `;
    container.appendChild(bar);
  } else if (trimmed > 0) {
    const warn = document.createElement('div');
    warn.className = 'ctx-trim-warning';
    warn.textContent = `⚠ 이전 ${trimmed}개 메시지가 토큰 한도 초과로 제거되었습니다`;
    container.appendChild(warn);
  }

  messages.forEach(msg => {
    const div = document.createElement('div');
    div.className = 'ctx-msg';

    if (msg.role === 'system') {
      div.innerHTML = `
        <div class="ctx-role ctx-role-system">system</div>
        <details class="ctx-system-details">
          <summary>시스템 프롬프트 (클릭하여 펼치기)</summary>
          <pre class="ctx-content">${esc(msg.content)}</pre>
        </details>`;

    } else if (msg.role === 'user') {
      const bgMatch  = msg.content.match(/^\[배경\]\s*([\s\S]*)$/);
      const spkMatch = msg.content.match(/^\[([^\]]+)\]\s*([\s\S]*)$/);

      if (bgMatch) {
        div.innerHTML = `
          <div class="ctx-role ctx-role-background">배경</div>
          <div class="ctx-content">${esc(bgMatch[1])}</div>`;
      } else if (spkMatch) {
        const inActionMatch = spkMatch[2].match(/^([\s\S]*?)\n\(([^)]+)\)\s*$/);
        const inContent    = inActionMatch ? inActionMatch[1] : spkMatch[2];
        const inActionNote = inActionMatch ? inActionMatch[2] : '';
        div.innerHTML = `
          <div class="ctx-role ctx-role-incoming">
            <span class="ctx-speaker-badge">${esc(spkMatch[1])}</span>incoming
          </div>
          <div class="ctx-content">${esc(inContent)}</div>
          ${inActionNote ? `<div class="sim-feed-action">*${esc(inActionNote)}*</div>` : ''}`;
      } else {
        div.innerHTML = `
          <div class="ctx-role ctx-role-incoming">user</div>
          <div class="ctx-content">${esc(msg.content)}</div>`;
      }

    } else if (msg.role === 'assistant') {
      let parsed = null;
      try {
        let raw = msg.content;
        if (raw.includes('```json')) raw = raw.split('```json')[1].split('```')[0];
        else if (raw.includes('```')) raw = raw.split('```')[1].split('```')[0];
        parsed = JSON.parse(raw.trim());
      } catch (_) {}

      if (parsed) {
        const tgt = Array.isArray(parsed.target) ? parsed.target.join(', ') : (parsed.target ?? '');
        const ctxActionNote = parsed.action_note || '';
        const metaBadges = sim.extra_fields
          .filter(f => f.name !== 'action_note' && parsed[f.name] != null)
          .map(f => {
            const val = String(parsed[f.name]);
            const cls = f.name === 'emotion' ? emotionClass(val) : 'emotion-neutral';
            return `<span class="sim-feed-badge ${cls}">${esc(val)}</span>`;
          }).join('');
        div.innerHTML = `
          <div class="ctx-role ctx-role-assistant">assistant</div>
          <div class="ctx-parsed-content">"${esc(parsed.content ?? '')}"</div>
          ${ctxActionNote ? `<div class="sim-feed-action">*${esc(ctxActionNote)}*</div>` : ''}
          <div class="ctx-parsed-meta">
            ${metaBadges}
            <span class="ctx-targets">→ ${esc(tgt || 'system')}</span>
          </div>`;
      } else {
        div.innerHTML = `
          <div class="ctx-role ctx-role-assistant">assistant</div>
          <div class="ctx-content">${esc(msg.content)}</div>`;
      }
    }

    container.appendChild(div);
  });

  container.lastElementChild?.scrollIntoView({ block: 'end' });
}

// ── Panel resize ──────────────────────────────────────────────────────────────
function initResizeHandles() {
  setupResize(
    document.getElementById('sim-resize-r'),
    document.getElementById('sim-graph-panel'),
    'left'
  );
}

function setupResize(handle, panel, growDir) {
  handle.addEventListener('mousedown', e => {
    const startX = e.clientX;
    const startW = panel.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = ev => {
      const dx   = growDir === 'right' ? ev.clientX - startX : startX - ev.clientX;
      const newW = Math.max(160, Math.min(700, startW + dx));
      panel.style.width    = `${newW}px`;
      panel.style.minWidth = `${newW}px`;
    };
    const onUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
    e.preventDefault();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────
export function initSimulationEvents() {
  document.getElementById('sim-btn').addEventListener('click', showSimView);
  document.getElementById('sim-back-btn').addEventListener('click', hideSimView);
  document.getElementById('sim-settings-btn').addEventListener('click', showSettingsView);
  document.getElementById('sim-settings-back-btn').addEventListener('click', hideSettingsView);
  document.getElementById('sim-new-scenario-btn').addEventListener('click', () => {
    if (sim.agents.length || sim.background) {
      if (!confirm('현재 설정을 초기화하고 새 시나리오를 만드시겠습니까?')) return;
    }
    newScenario();
  });
  document.getElementById('sim-settings-start-btn').addEventListener('click', () => {
    hideSettingsView();
    startSimulation();
  });
  document.getElementById('sim-start-btn').addEventListener('click', startSimulation);
  document.getElementById('sim-continue-btn').addEventListener('click', continueSimulation);
  document.getElementById('sim-stop-btn').addEventListener('click', stopSimulation);
  document.getElementById('sim-save-scenario-btn').addEventListener('click', saveScenario);
  document.getElementById('sim-delete-scenario-btn').addEventListener('click', deleteScenario);
  document.getElementById('sim-history-btn')?.addEventListener('click', toggleRunHistory);
  document.getElementById('sim-all-runs-btn').addEventListener('click', openAllRunsModal);
  document.getElementById('sim-export-graph-btn').addEventListener('click', exportGraph);

  document.getElementById('sim-add-agent-btn').addEventListener('click', () => {
    const newName = `agent${sim.agents.length + 1}`;
    sim.agents.push({ name: newName, display_name: '', icon: '🤖', system_prompt: '', initial_active: true });
    _expandedAgents.add(newName);
    renderAgentListInConfig();
    renderStartAgentSelect();
  });

  document.getElementById('sim-add-field-btn').addEventListener('click', () => {
    sim.extra_fields.push({ name: '', default: '' });
    renderOutputFields();
  });

  document.getElementById('sim-add-event-btn').addEventListener('click', () => {
    sim.events.push({ wave: 1, type: 'system_message', message: '', targets: ['all'], agent: '' });
    renderScenarioEvents();
  });

  document.querySelectorAll('.sim-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('sim-context-refresh-btn').addEventListener('click', () => {
    if (sim.selectedAgent) fetchAgentContext(sim.selectedAgent);
  });

  initResizeHandles();

  document.getElementById('sim-start-agent').addEventListener('change', e => {
    sim.start_agent = e.target.value;
  });

  document.getElementById('sim-scenario-select').addEventListener('change', e => {
    const found = sim.scenarios.find(s => s.id === e.target.value);
    if (found) { applyScenario(found); e.target.value = ''; }
  });

  document.getElementById('sim-reset-format-btn').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/simulation/default-output-format');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      document.getElementById('sim-output-format').value = data.template;
      sim.output_format_template = data.template;
    } catch (e) {
      console.error('[sim] 기본 출력 포맷 불러오기 실패:', e);
    }
  });
}
