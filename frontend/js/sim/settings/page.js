// frontend/js/sim/settings/page.js
// Top-level orchestration for the settings page (form ↔ sim state sync).

import { sim } from '../state.js';
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
  // system 에이전트 설정 읽기
  sim.system_agent = {
    enabled:               document.getElementById('sim-sys-enabled')?.checked       ?? false,
    icon:                  document.getElementById('sim-sys-icon')?.value.trim()     || '🎬',
    display_name:          document.getElementById('sim-sys-display-name')?.value.trim() || '내레이터',
    system_prompt:         document.getElementById('sim-sys-prompt')?.value          || '',
    intervention_interval: parseInt(document.getElementById('sim-sys-interval')?.value) || 1,
    silence_threshold:     parseInt(document.getElementById('sim-sys-silence')?.value)  || 3,
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

  document.getElementById('sim-sys-icon').value         = sa.icon         || '🎬';
  document.getElementById('sim-sys-display-name').value = sa.display_name || '내레이터';
  document.getElementById('sim-sys-prompt').value       = sa.system_prompt || '';
  document.getElementById('sim-sys-interval').value     = sa.intervention_interval ?? 1;
  document.getElementById('sim-sys-silence').value      = sa.silence_threshold    ?? 3;

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
