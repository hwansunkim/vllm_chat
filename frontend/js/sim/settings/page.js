// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).

import { sim, esc, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday } from '../state.js';
import { renderOutputFields } from './output-fields.js';
import { renderAgentListInConfig, renderStartAgentSelect } from './agents.js';
import { renderScenarioEvents } from './events.js';
import { getServerList, invalidateServerList } from './server-list.js';

export function renderSettingsPage() {
  // 설정 패널을 열 때마다 서버 목록을 새로 읽는다 — 그 사이 서버 모달에서
  // 추가/삭제된 서버가 드롭다운에 반영되어야 하므로. 아래 시뮬레이션 레벨
  // 드롭다운과 에이전트별 드롭다운들이 이 한 번의 fetch를 공유한다.
  invalidateServerList();
  document.getElementById('sim-scenario-name').value  = sim.currentScenarioName;
  document.getElementById('sim-background').value     = sim.background;
  document.getElementById('sim-max-waves').value      = sim.max_waves;
  document.getElementById('sim-step-delay').value     = sim.step_delay;
  document.getElementById('sim-token-limit').value    = sim.token_limit;
  document.getElementById('sim-llm-max-tokens').value = sim.llm_max_tokens;
  document.getElementById('sim-output-format').value    = sim.output_format_template || '';
  document.getElementById('sim-summary-interval').value = sim.summary_interval ?? 0;
  const startTimeEl = document.getElementById('sim-start-time');
  if (startTimeEl) startTimeEl.value = sim.sim_start_time ?? '09:00';
  const startWeekdayEl = document.getElementById('sim-start-weekday');
  if (startWeekdayEl) startWeekdayEl.value = normalizeWeekday(sim.sim_start_weekday);
  const timePerWaveEl = document.getElementById('sim-time-per-wave');
  if (timePerWaveEl) timePerWaveEl.value = sim.time_per_wave ?? 30;
  const timeModeEl = document.getElementById('sim-time-mode');
  if (timeModeEl) {
    timeModeEl.value = sim.time_mode === 'variable' ? 'variable' : 'fixed';
    _updateVariableTimeUI(timeModeEl.value);
  }
  _renderTimeCategories();
  const idleScheduleEl = document.getElementById('sim-idle-schedule');
  if (idleScheduleEl) {
    const sched = sim.idle_minutes_schedule?.length ? sim.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE;
    idleScheduleEl.value = sched.join(',');
  }
  const maxSilenceEl = document.getElementById('sim-max-silence-waves');
  if (maxSilenceEl) maxSilenceEl.value = sim.max_silence_waves ?? 3;
  const earlyStopEl = document.getElementById('sim-early-stop-enabled');
  if (earlyStopEl) {
    earlyStopEl.checked = sim.early_stop_enabled ?? true;
    _updateEarlyStopUI(earlyStopEl.checked);
  }
  const langFixEl = document.getElementById('sim-lang-fix-enabled');
  if (langFixEl) langFixEl.checked = sim.lang_fix_enabled ?? true;
  const langRetEl = document.getElementById('sim-lang-fix-retries');
  if (langRetEl) langRetEl.value = sim.lang_fix_retries ?? 2;
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
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)     || 8192;
  sim.llm_max_tokens         = parseInt(document.getElementById('sim-llm-max-tokens').value) || 16384;
  sim.output_format_template = document.getElementById('sim-output-format').value;
  sim.summary_interval       = parseInt(document.getElementById('sim-summary-interval').value) || 0;
  sim.sim_start_time    = document.getElementById('sim-start-time')?.value || '09:00';
  sim.sim_start_weekday = normalizeWeekday(document.getElementById('sim-start-weekday')?.value);
  sim.time_per_wave     = parseInt(document.getElementById('sim-time-per-wave')?.value)     || 0;
  sim.time_mode         = document.getElementById('sim-time-mode')?.value === 'variable' ? 'variable' : 'fixed';
  sim.time_categories   = _readTimeCategories();
  sim.idle_minutes_schedule = _readIdleSchedule();
  sim.max_silence_waves  = parseInt(document.getElementById('sim-max-silence-waves')?.value)  || 3;
  sim.early_stop_enabled = document.getElementById('sim-early-stop-enabled')?.checked ?? true;
  const sel = document.getElementById('sim-server-select');
  sim.server_id              = sel?.value || null;
  sim.location_graph   = _readLocationGraph();
  sim.lang_fix_enabled = document.getElementById('sim-lang-fix-enabled')?.checked ?? true;
  sim.lang_fix_retries = parseInt(document.getElementById('sim-lang-fix-retries')?.value) || 2;
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

// ── 조기 종료 토글 UI 연동 ────────────────────────────────────────────────────

function _updateEarlyStopUI(enabled) {
  const silenceInput = document.getElementById('sim-max-silence-waves');
  if (silenceInput) silenceInput.disabled = !enabled;
}

export function initEarlyStopToggle() {
  const chk = document.getElementById('sim-early-stop-enabled');
  if (!chk) return;
  chk.onchange = () => {
    sim.early_stop_enabled = chk.checked;
    _updateEarlyStopUI(chk.checked);
  };
}

// ── 가변 시간 모드 UI 연동 ────────────────────────────────────────────────────

function _updateVariableTimeUI(mode) {
  const section = document.getElementById('sim-variable-time-section');
  if (section) section.classList.toggle('sim-hidden', mode !== 'variable');
}

function _renderTimeCategories() {
  const cats = sim.time_categories?.length ? sim.time_categories : DEFAULT_TIME_CATEGORIES;
  document.querySelectorAll('.sim-time-cat-row').forEach(row => {
    const id = row.dataset.catId;
    const cat = cats.find(c => c.id === id) || DEFAULT_TIME_CATEGORIES.find(c => c.id === id);
    if (!cat) return;
    const labelEl = row.querySelector('.sim-time-cat-label');
    const minEl   = row.querySelector('.sim-time-cat-min');
    const maxEl   = row.querySelector('.sim-time-cat-max');
    if (labelEl) labelEl.value = cat.label;
    if (minEl)   minEl.value   = cat.min_minutes;
    if (maxEl)   maxEl.value   = cat.max_minutes;
  });
}

// 카테고리는 항상 4개 슬롯이 DOM에 고정되어 있으므로 빈 배열이 나올 일은 없지만,
// 방어적으로 비어 있으면 기본값으로 폴백한다 (백엔드가 [] 를 422로 거부함).
function _readTimeCategories() {
  const rows = document.querySelectorAll('.sim-time-cat-row');
  const result = [];
  rows.forEach(row => {
    const id = row.dataset.catId;
    if (!id) return;
    const fallback = DEFAULT_TIME_CATEGORIES.find(c => c.id === id) || {};
    const labelEl  = row.querySelector('.sim-time-cat-label');
    const minEl    = row.querySelector('.sim-time-cat-min');
    const maxEl    = row.querySelector('.sim-time-cat-max');
    const label    = labelEl?.value.trim() || fallback.label || id;
    let min = parseInt(minEl?.value);
    if (isNaN(min) || min < 1) min = fallback.min_minutes ?? 5;
    let max = parseInt(maxEl?.value);
    if (isNaN(max) || max < min) max = Math.max(min, fallback.max_minutes ?? min);
    result.push({ id, label, min_minutes: min, max_minutes: max });
  });
  return result.length ? result : DEFAULT_TIME_CATEGORIES.map(c => ({ ...c }));
}

// idle_minutes_schedule: 콤마 구분 텍스트 → 정수 배열. 빈 배열이 되면 안 됨(백엔드 422 방지).
function _readIdleSchedule() {
  const raw = document.getElementById('sim-idle-schedule')?.value || '';
  const nums = raw.split(',')
    .map(s => parseInt(s.trim()))
    .filter(n => !isNaN(n) && n > 0);
  return nums.length ? nums : [...DEFAULT_IDLE_MINUTES_SCHEDULE];
}

export function initTimeModeToggle() {
  const sel = document.getElementById('sim-time-mode');
  if (!sel) return;
  sel.onchange = () => {
    sim.time_mode = sel.value === 'variable' ? 'variable' : 'fixed';
    _updateVariableTimeUI(sim.time_mode);
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

  const servers = await getServerList();

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
    const isExt = !!node.is_exterior;
    row.className = `sim-loc-node-row${isExt ? ' sim-loc-exterior' : ''}`;
    const connBadges = (node.connects_to || []).map(c => `
      <span class="sim-loc-conn-badge">
        ${esc(c)}
        <button class="sim-loc-conn-del" data-node="${idx}" data-conn="${esc(c)}" title="연결 제거">×</button>
      </span>`).join('');
    const otherNodes = nodeNames.filter(n => n !== node.name && !(node.connects_to || []).includes(n));
    const addConnOpts = otherNodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    row.innerHTML = `
      <div class="sim-loc-node-header">
        <span class="sim-loc-node-icon">${isExt ? '🌐' : '📍'}</span>
        <input class="sim-loc-node-name" data-idx="${idx}" value="${esc(node.name)}" placeholder="장소 이름" />
        <label class="sim-loc-exterior-toggle" title="외부 공간 — 이 장소에 있는 에이전트는 서로를 볼 수 없고 내부와 소통 불가">
          <input type="checkbox" class="sim-loc-exterior-chk" data-idx="${idx}" ${isExt ? 'checked' : ''}>
          <span>외부</span>
        </label>
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
    const extChk = e.target.closest('.sim-loc-exterior-chk');
    if (extChk) {
      const i = parseInt(extChk.dataset.idx);
      sim.location_graph[i].is_exterior = extChk.checked;
      renderLocationGraph();
      return;
    }
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
  return (sim.location_graph || []).map(n => ({
    name:        n.name,
    connects_to: [...(n.connects_to || [])],
    is_exterior: !!n.is_exterior,
  }));
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [] });
  renderLocationGraph();
}
