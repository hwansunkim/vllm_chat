// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).

import { sim, esc } from '../state.js';
import { renderOutputFields } from './output-fields.js';
import { renderAgentListInConfig, renderStartAgentSelect } from './agents.js';
import { renderScenarioEvents } from './events.js';

export function renderSettingsPage() {
  document.getElementById('sim-scenario-name').value  = sim.currentScenarioName;
  document.getElementById('sim-background').value     = sim.background;
  document.getElementById('sim-max-waves').value      = sim.max_waves;
  document.getElementById('sim-step-delay').value     = sim.step_delay;
  document.getElementById('sim-token-limit').value    = sim.token_limit;
  document.getElementById('sim-output-format').value    = sim.output_format_template || '';
  document.getElementById('sim-summary-interval').value = sim.summary_interval ?? 0;
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = !sim.currentScenarioId;
  renderOutputFields();
  renderAgentListInConfig();
  renderStartAgentSelect();
  renderScenarioEvents();
  renderLocationGraph();
  renderServerSelect();        // 비동기 — 드롭다운 별도 렌더링
  renderSystemAgentConfig();   // system 에이전트 설정 동기 렌더링
}

export function readConfigFromUI() {
  sim.background             = document.getElementById('sim-background').value.trim();
  sim.start_agent            = document.getElementById('sim-start-agent').value;
  sim.max_waves              = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.step_delay             = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)  || 8192;
  sim.output_format_template = document.getElementById('sim-output-format').value;
  sim.summary_interval       = parseInt(document.getElementById('sim-summary-interval').value) || 0;
  const sel = document.getElementById('sim-server-select');
  sim.server_id              = sel?.value || null;
  sim.location_graph = _readLocationGraph();
  // system 에이전트 설정 읽기
  sim.system_agent = {
    enabled:               document.getElementById('sim-sys-enabled')?.checked           ?? false,
    icon:                  document.getElementById('sim-sys-icon')?.value.trim()         || '🎬',
    display_name:          document.getElementById('sim-sys-display-name')?.value.trim() || '내레이터',
    system_prompt:         document.getElementById('sim-sys-prompt')?.value              || '',
    intervention_interval: parseInt(document.getElementById('sim-sys-interval')?.value)  || 1,
    silence_threshold:     parseInt(document.getElementById('sim-sys-silence')?.value)   || 3,
    director_note:         document.getElementById('sim-sys-director-note')?.value       || '',
  };
}

// ── system 에이전트 설정 렌더링 ───────────────────────────────────────────────

function renderSystemAgentConfig() {
  const sa      = sim.system_agent || {};
  const enabled = !!sa.enabled;
  const chk     = document.getElementById('sim-sys-enabled');
  const cfg     = document.getElementById('sim-sys-config');
  if (!chk || !cfg) return;

  chk.checked = enabled;
  cfg.classList.toggle('sim-hidden', !enabled);

  document.getElementById('sim-sys-icon').value          = sa.icon                  || '🎬';
  document.getElementById('sim-sys-display-name').value  = sa.display_name          || '내레이터';
  document.getElementById('sim-sys-prompt').value        = sa.system_prompt         || '';
  document.getElementById('sim-sys-interval').value      = sa.intervention_interval ?? 1;
  document.getElementById('sim-sys-silence').value       = sa.silence_threshold     ?? 3;
  document.getElementById('sim-sys-director-note').value = sa.director_note         || '';

  chk.onchange = () => {
    sim.system_agent.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
  };
}

// ── 서버 드롭다운 ─────────────────────────────────────────────────────────────

async function renderServerSelect() {
  const sel  = document.getElementById('sim-server-select');
  const hint = document.getElementById('sim-server-hint');
  if (!sel) return;

  let servers = [];
  try {
    const res = await fetch('/api/servers');
    if (res.ok) servers = await res.json();
  } catch (_) {}

  sel.innerHTML = `<option value="">기본 서버</option>` +
    servers.map(s =>
      `<option value="${s.id}">${s.name}</option>`
    ).join('');

  // 저장된 server_id로 선택값 복원
  sel.value = sim.server_id || '';

  updateServerHint(sel.value, servers);

  sel.onchange = () => {
    sim.server_id = sel.value || null;
    updateServerHint(sel.value, servers);
  };
}

function updateServerHint(serverId, servers) {
  const hint = document.getElementById('sim-server-hint');
  if (!hint) return;
  if (!serverId) {
    const def = servers.find(s => s.is_default);
    hint.textContent = def
      ? `기본: ${def.model.split('/').pop()} · ${def.base_url}`
      : '등록된 서버가 없으면 환경변수 설정을 사용합니다';
  } else {
    const s = servers.find(s => s.id === serverId);
    hint.textContent = s ? `${s.model.split('/').pop()} · ${s.base_url}` : '';
  }
}

// ── 위치 그래프 에디터 ─────────────────────────────────────────────────────────

function renderLocationGraph() {
  const container = document.getElementById('sim-location-graph');
  if (!container) return;
  const graph = sim.location_graph || [];
  const nodeNames = graph.map(n => n.name);
  container.innerHTML = '';

  graph.forEach((node, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-loc-node-row';
    const connBadges = (node.connects_to || []).map(c => `
      <span class="sim-loc-conn-badge">
        ${esc(c)}
        <button class="sim-loc-conn-del" data-node="${idx}" data-conn="${esc(c)}" title="연결 제거">×</button>
      </span>`).join('');
    const otherNodes = nodeNames.filter(n => n !== node.name && !(node.connects_to || []).includes(n));
    const addConnOpts = otherNodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    row.innerHTML = `
      <div class="sim-loc-node-header">
        <span class="sim-loc-node-icon">📍</span>
        <input class="sim-loc-node-name" data-idx="${idx}" value="${esc(node.name)}" placeholder="장소 이름" />
        <button class="sim-loc-node-del" data-idx="${idx}" title="장소 삭제">×</button>
      </div>
      <div class="sim-loc-conns">
        <span class="sim-loc-conns-label">연결:</span>
        ${connBadges || '<span class="sim-loc-no-conn">(없음)</span>'}
        ${addConnOpts ? `<select class="sim-loc-add-conn" data-node="${idx}">
          <option value="">+ 연결 추가</option>
          ${addConnOpts}
        </select>` : ''}
      </div>`;
    container.appendChild(row);
  });

  // 이벤트 위임
  container.onclick = e => {
    const delNode = e.target.closest('.sim-loc-node-del');
    if (delNode) {
      const i = parseInt(delNode.dataset.idx);
      const name = sim.location_graph[i].name;
      sim.location_graph.splice(i, 1);
      sim.location_graph.forEach(n => {
        n.connects_to = (n.connects_to || []).filter(c => c !== name);
      });
      renderLocationGraph();
      return;
    }
    const delConn = e.target.closest('.sim-loc-conn-del');
    if (delConn) {
      const ni = parseInt(delConn.dataset.node);
      const conn = delConn.dataset.conn;
      sim.location_graph[ni].connects_to = (sim.location_graph[ni].connects_to || []).filter(c => c !== conn);
      const other = sim.location_graph.find(n => n.name === conn);
      if (other) other.connects_to = (other.connects_to || []).filter(c => c !== sim.location_graph[ni].name);
      renderLocationGraph();
      return;
    }
  };

  container.onchange = e => {
    const nameInput = e.target.closest('.sim-loc-node-name');
    if (nameInput) {
      const i = parseInt(nameInput.dataset.idx);
      const oldName = sim.location_graph[i].name;
      const newName = nameInput.value.trim();
      if (newName && newName !== oldName) {
        sim.location_graph.forEach(n => {
          n.connects_to = (n.connects_to || []).map(c => c === oldName ? newName : c);
        });
        sim.location_graph[i].name = newName;
        renderLocationGraph();
      }
      return;
    }
    const addConn = e.target.closest('.sim-loc-add-conn');
    if (addConn) {
      const ni = parseInt(addConn.dataset.node);
      const target = addConn.value;
      if (!target) return;
      const nodeA = sim.location_graph[ni];
      const nodeB = sim.location_graph.find(n => n.name === target);
      if (!nodeA.connects_to) nodeA.connects_to = [];
      if (!nodeA.connects_to.includes(target)) nodeA.connects_to.push(target);
      if (nodeB) {
        if (!nodeB.connects_to) nodeB.connects_to = [];
        if (!nodeB.connects_to.includes(nodeA.name)) nodeB.connects_to.push(nodeA.name);
      }
      renderLocationGraph();
      return;
    }
  };
}

function _readLocationGraph() {
  return (sim.location_graph || []).map(n => ({ name: n.name, connects_to: [...(n.connects_to || [])] }));
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [] });
  renderLocationGraph();
}
