import { esc } from './utils.js';

// ── Module state ──────────────────────────────────────────────────────────────
const sim = {
  status:        'idle',
  selectedAgent: null,
  agents: [
    { name: 'boss',     display_name: '김민태', icon: '😤', initial_active: true,  system_prompt: "너는 편의점 사장님 '김민태'야. 47세 남자. 욕심이 많고 고지식함. 말투가 거칠고 불평이 많음." },
    { name: 'lee',      display_name: '이상민', icon: '😊', initial_active: true,  system_prompt: "너는 편의점 알바생 '이상민'이야. 29세 남자. 아이돌 지망생. 착실하고 씩씩하며 친절한 말투." },
    { name: 'park',     display_name: '박슬기', icon: '😒', initial_active: true,  system_prompt: "너는 편의점 알바생 '박슬기'야. 21세 여자. 게으르고 냉소적임. 말투가 거칠고 반항적임." },
    { name: 'customer', display_name: '정용진', icon: '😠', initial_active: false, system_prompt: "너는 편의점 손님 '정용진'이야. 55세 남자. 불만이 많고 괴팍함. 말투가 거칠고 막무가내임." },
  ],
  background:   '편의점 아침 출근 시간. 김민태 사장님이 도착하여 문을 열었다. 이상민과 박슬기 알바생이 출근했다.',
  start_agent:  'boss',
  max_waves:    10,
  step_delay:   1.0,
  memory_limit: 20,
  events: [
    { wave: 2, type: 'agent_enter', agent: 'customer', message: '편의점 손님 정용진이 들어왔다.', targets: ['all'] },
  ],
  eventSource:  null,
  scenarios:    [],
};

// ── Emotion helpers ───────────────────────────────────────────────────────────
const EMOTION_COLORS = {
  angry: '#ef4444', happy: '#22c55e', neutral: '#94a3b8',
  sad: '#3b82f6', fear: '#f97316',
};
const EMOTION_CLASS = ['angry','happy','neutral','sad','fear'];

function emotionColor(e) { return EMOTION_COLORS[e] || '#a78bfa'; }
function emotionClass(e) { return EMOTION_CLASS.includes(e) ? `emotion-${e}` : 'emotion-neutral'; }

// ── View toggle ───────────────────────────────────────────────────────────────
function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  renderConfigPanel();
  renderAgentCards();
  initD3Graph();
  loadScenarios();
}

function hideSimView() {
  document.getElementById('sim-view').classList.add('sim-hidden');
  document.getElementById('main').classList.remove('sim-hidden');
}

// ── Config panel ──────────────────────────────────────────────────────────────
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
  // 이전 선택값 복원, 없으면 첫 번째
  if (prev && sim.agents.find(a => a.name === prev)) sel.value = prev;
  else if (sim.agents.length) sel.value = sim.agents[0].name;
  sim.start_agent = sel.value;
}

function renderConfigPanel() {
  document.getElementById('sim-background').value   = sim.background;
  document.getElementById('sim-max-waves').value    = sim.max_waves;
  document.getElementById('sim-step-delay').value   = sim.step_delay;
  document.getElementById('sim-memory-limit').value = sim.memory_limit;
  renderAgentListInConfig();
  renderStartAgentSelect();
  renderScenarioEvents();
}

function renderAgentListInConfig() {
  const list = document.getElementById('sim-agent-list');
  list.innerHTML = '';

  sim.agents.forEach((agent, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-agent-row';
    const isActive = agent.initial_active !== false;
    row.innerHTML = `
      <input class="sim-agent-icon-input" data-idx="${idx}" data-field="icon"
             value="${esc(agent.icon)}" maxlength="4" title="아이콘 (이모지)"/>
      <div class="sim-agent-info">
        <div class="sim-agent-name-row">
          <input class="sim-agent-name-input" data-idx="${idx}" data-field="name"
                 value="${esc(agent.name)}" placeholder="ID (영문)" title="시스템 ID"/>
          <input class="sim-agent-display-name-input" data-idx="${idx}" data-field="display_name"
                 value="${esc(agent.display_name || '')}" placeholder="표시이름 (선택)" title="한국어 이름 등 — LLM이 이 이름으로 target 지정해도 올바른 ID로 연결됩니다"/>
        </div>
        <textarea class="sim-agent-prompt-input" data-idx="${idx}" data-field="system_prompt"
                  placeholder="시스템 프롬프트">${esc(agent.system_prompt)}</textarea>
      </div>
      <div class="sim-agent-active-wrap" title="처음부터 등장">
        <label class="sim-active-toggle">
          <input type="checkbox" data-idx="${idx}" data-field="initial_active" ${isActive ? 'checked' : ''}/>
          <span class="track"></span><span class="thumb"></span>
        </label>
        <label style="font-size:9px;color:#94a3b8">${isActive ? '초기' : '대기'}</label>
      </div>
      <button class="sim-agent-del" data-idx="${idx}" title="삭제">✕</button>
    `;
    list.appendChild(row);
  });

  const addBtn = document.createElement('button');
  addBtn.className = 'sim-add-agent-btn';
  addBtn.textContent = '+ 에이전트 추가';
  addBtn.onclick = () => {
    sim.agents.push({ name: `agent${sim.agents.length + 1}`, display_name: '', icon: '🤖', system_prompt: '', initial_active: true });
    renderAgentListInConfig();
    renderStartAgentSelect();
    renderAgentCards();
  };
  list.appendChild(addBtn);

  list.querySelectorAll('[data-field]').forEach(el => {
    el.addEventListener('input', () => {
      const idx = +el.dataset.idx;
      if (el.type === 'checkbox') {
        sim.agents[idx][el.dataset.field] = el.checked;
        // 라벨 텍스트 갱신
        el.closest('.sim-agent-active-wrap')
          .querySelector('label:last-child').textContent = el.checked ? '초기' : '대기';
        renderScenarioEvents(); // agent 선택 목록 갱신
      } else {
        sim.agents[idx][el.dataset.field] = el.value;
        if (el.dataset.field === 'name') { renderStartAgentSelect(); renderScenarioEvents(); }
      }
    });
  });
  list.querySelectorAll('.sim-agent-del').forEach(el => {
    el.addEventListener('click', () => {
      sim.agents.splice(+el.dataset.idx, 1);
      renderAgentListInConfig();
      renderStartAgentSelect();
      renderAgentCards();
    });
  });
}

function readConfigFromUI() {
  sim.background   = document.getElementById('sim-background').value.trim();
  sim.start_agent  = document.getElementById('sim-start-agent').value;
  sim.max_waves    = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.step_delay   = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.memory_limit = parseInt(document.getElementById('sim-memory-limit').value) || 20;
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
    const displayLabel = agent.display_name ? `${esc(agent.display_name)}<small style="color:#94a3b8;font-weight:400"> (${esc(agent.name)})</small>` : esc(agent.name);
    card.innerHTML = `
      <div class="sim-card-header">
        <span class="sim-card-icon">${esc(agent.icon)}</span>
        <span class="sim-card-name">${displayLabel}</span>
      </div>
      <span class="sim-card-emotion emotion-neutral" id="simc-em-${esc(agent.name)}">neutral</span>
      <div class="sim-card-action"  id="simc-ac-${esc(agent.name)}">speak</div>
      <div class="sim-card-mem-wrap">
        <div class="sim-card-mem-fill" id="simc-mem-${esc(agent.name)}" style="width:0%"></div>
      </div>
      <div class="sim-card-preview" id="simc-pre-${esc(agent.name)}">대기 중...</div>
    `;
    card.addEventListener('click', () => openAgentContext(agent.name));
    container.appendChild(card);
  });
}

function updateAgentCard(speaker, emotion, action, memorySize, preview) {
  const emEl = document.getElementById(`simc-em-${speaker}`);
  if (emEl) { emEl.textContent = emotion; emEl.className = `sim-card-emotion ${emotionClass(emotion)}`; }

  const acEl = document.getElementById(`simc-ac-${speaker}`);
  if (acEl) acEl.textContent = action;

  const memEl = document.getElementById(`simc-mem-${speaker}`);
  if (memEl) memEl.style.width = `${Math.min(100, (memorySize / sim.memory_limit) * 100)}%`;

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
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(agent.icon)} ${esc(agent.name)}</span>
      <span class="sim-feed-target">${esc(targetStr)}</span>
    </div>
    <div class="sim-feed-bubble">${esc(data.content)}</div>
    <div class="sim-feed-meta">
      <span class="sim-feed-badge ${emotionClass(data.emotion)}">${esc(data.emotion)}</span>
      <span class="sim-feed-badge emotion-neutral">${esc(data.action)}</span>
    </div>
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
    // 이전 wave typing 인디케이터 전체 제거
    removeTypingIndicator();
    // 이전 speaking 상태 초기화
    document.querySelectorAll('.sim-agent-card').forEach(c => c.classList.remove('speaking'));
    // 이번 wave 에이전트 하이라이트
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
    updateAgentCard(d.speaker, d.emotion, d.action, d.memory_size, d.content);
    d.new_edges?.forEach(edge => addD3Edge(edge.source, edge.target, edge.emotion));
    document.getElementById(`simc-${d.speaker}`)?.classList.remove('speaking');
    // context 탭이 열려 있고 이 발화자가 선택된 경우 자동 갱신
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
      agents:       sim.agents,
      background:   sim.background,
      start_agent:  sim.start_agent,
      max_waves:    sim.max_waves,
      step_delay:   sim.step_delay,
      memory_limit: sim.memory_limit,
      events:       sim.events,
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
  document.getElementById('sim-start-btn').disabled = status === 'running';
  document.getElementById('sim-stop-btn').disabled  = status !== 'running';
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
}

async function saveScenario() {
  readConfigFromUI();
  const nameEl = document.getElementById('sim-scenario-name');
  const name   = nameEl.value.trim();
  if (!name) { nameEl.focus(); nameEl.style.borderColor = '#ef4444'; return; }
  nameEl.style.borderColor = '';

  const res = await fetch('/api/simulation/scenarios', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name,
      description: '',
      config: {
        agents:       sim.agents,
        background:   sim.background,
        start_agent:  sim.start_agent,
        max_waves:    sim.max_waves,
        step_delay:   sim.step_delay,
        memory_limit: sim.memory_limit,
        events:       sim.events,
      },
    }),
  });

  if (res.ok) {
    nameEl.value = '';
    await loadScenarios();
  }
}

function applyScenario(s) {
  const cfg = s.config;
  sim.agents       = cfg.agents       || [];
  sim.background   = cfg.background   || '';
  sim.start_agent  = cfg.start_agent  || (cfg.agents?.[0]?.name ?? '');
  sim.max_waves    = cfg.max_waves    || 10;
  sim.step_delay   = cfg.step_delay   || 1.0;
  sim.memory_limit = cfg.memory_limit || 20;
  sim.events       = cfg.events       || [];
  renderConfigPanel();
  renderAgentCards();
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

  // wave 순으로 정렬 후 렌더링
  const sorted = sim.events
    .map((e, i) => ({ ...e, _i: i }))
    .sort((a, b) => a.wave - b.wave);

  sorted.forEach(({ _i: idx }) => {
    const ev = sim.events[idx];
    const row = document.createElement('div');
    row.className = 'sim-event-row';

    // 에이전트 등장/퇴장용 선택 목록
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

  // 입력 이벤트
  list.querySelectorAll('[data-field]').forEach(el => {
    el.addEventListener('change', () => syncEventField(el));
    el.addEventListener('input',  () => {
      if (el.dataset.field === 'type') syncEventField(el); // type 변경 시 즉시 재렌더
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
    renderScenarioEvents(); // 필드 구성이 달라지므로 재렌더
  } else if (field === 'targets_str') {
    sim.events[idx].targets = el.value.split(',').map(s => s.trim()).filter(Boolean);
    if (!sim.events[idx].targets.length) sim.events[idx].targets = ['all'];
  } else {
    sim.events[idx][field] = el.value;
  }
}

function addSceneEventToFeed(d) {
  removeFeedEmpty();
  const icons = { system_message: '📢', agent_enter: '🎭', agent_exit: '🚪' };
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
  // 컨텍스트 탭으로 전환할 때 항상 최신 상태로 갱신
  if (tabName === 'context' && sim.selectedAgent && sim.status !== 'idle') {
    fetchAgentContext(sim.selectedAgent);
  }
}

// ── Context window ────────────────────────────────────────────────────────────
async function openAgentContext(name) {
  sim.selectedAgent = name;
  switchTab('context');
  const a = sim.agents.find(ag => ag.name === name);
  const label = a ? `${a.icon} ${a.display_name || a.name}${a.display_name ? ` (${a.name})` : ''}` : `🤖 ${name}`;
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
    renderContextMessages(data.messages, data.trimmed || 0);
  } catch (e) {
    msgs.innerHTML = `<div style="padding:12px;font-size:11px;color:#ef4444;">오류: ${esc(String(e))}</div>`;
  }
}

function renderContextMessages(messages, trimmed = 0) {
  const container = document.getElementById('sim-context-msgs');
  container.innerHTML = '';

  if (trimmed > 0) {
    const warn = document.createElement('div');
    warn.className = 'ctx-trim-warning';
    warn.textContent = `⚠ 메모리 한도 초과: 이전 ${trimmed}개 메시지가 제거되었습니다 (시나리오 피드에는 전체 기록이 남아있음)`;
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
        div.innerHTML = `
          <div class="ctx-role ctx-role-incoming">
            <span class="ctx-speaker-badge">${esc(spkMatch[1])}</span>incoming
          </div>
          <div class="ctx-content">${esc(spkMatch[2])}</div>`;
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
        div.innerHTML = `
          <div class="ctx-role ctx-role-assistant">assistant</div>
          <div class="ctx-parsed-content">"${esc(parsed.content ?? '')}"</div>
          <div class="ctx-parsed-meta">
            <span class="sim-feed-badge ${emotionClass(parsed.emotion)}">${esc(parsed.emotion ?? '')}</span>
            <span class="sim-feed-badge emotion-neutral">${esc(parsed.action ?? '')}</span>
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

  // 마지막 메시지로 스크롤
  container.lastElementChild?.scrollIntoView({ block: 'end' });
}

// ── Panel resize ──────────────────────────────────────────────────────────────
function initResizeHandles() {
  setupResize(
    document.getElementById('sim-resize-l'),
    document.getElementById('sim-config'),
    'right'   // 오른쪽으로 드래그 → config 패널 확장
  );
  setupResize(
    document.getElementById('sim-resize-r'),
    document.getElementById('sim-graph-panel'),
    'left'    // 왼쪽으로 드래그 → 우측 패널 확장
  );
}

function setupResize(handle, panel, growDir) {
  handle.addEventListener('mousedown', e => {
    const startX = e.clientX;
    const startW = panel.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor    = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = ev => {
      const dx   = growDir === 'right' ? ev.clientX - startX : startX - ev.clientX;
      const newW = Math.max(160, Math.min(700, startW + dx));
      panel.style.width    = `${newW}px`;
      panel.style.minWidth = `${newW}px`;
    };
    const onUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor    = '';
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
  document.getElementById('sim-start-btn').addEventListener('click', startSimulation);
  document.getElementById('sim-stop-btn').addEventListener('click', stopSimulation);
  document.getElementById('sim-save-scenario-btn').addEventListener('click', saveScenario);
  document.getElementById('sim-export-graph-btn').addEventListener('click', exportGraph);

  // 시나리오 이벤트 추가
  document.getElementById('sim-add-event-btn').addEventListener('click', () => {
    sim.events.push({ wave: 1, type: 'system_message', message: '', targets: ['all'], agent: '' });
    renderScenarioEvents();
  });

  // 탭 전환
  document.querySelectorAll('.sim-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // context refresh
  document.getElementById('sim-context-refresh-btn').addEventListener('click', () => {
    if (sim.selectedAgent) fetchAgentContext(sim.selectedAgent);
  });

  // resize handles
  initResizeHandles();

  document.getElementById('sim-start-agent').addEventListener('change', e => {
    sim.start_agent = e.target.value;
  });

  document.getElementById('sim-scenario-select').addEventListener('change', e => {
    const found = sim.scenarios.find(s => s.id === e.target.value);
    if (found) { applyScenario(found); e.target.value = ''; }
  });
}
