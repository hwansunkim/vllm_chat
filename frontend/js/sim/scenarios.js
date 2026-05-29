// frontend/js/sim/scenarios.js
// Scenario list CRUD + current-scenario selection.

import { sim, _expandedAgents } from './state.js';
import { renderSettingsPage, readConfigFromUI } from './settings/page.js';
import { refreshRunHistory } from './runs/history.js';

export async function loadScenarios() {
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

export async function saveScenario() {
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

export async function deleteScenario() {
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

export function newScenario() {
  sim.currentScenarioId   = null;
  sim.currentScenarioName = '';
  sim.agents              = [];
  sim.background          = '';
  sim.start_agent         = '';
  sim.max_waves           = 10;
  sim.step_delay          = 1.0;
  sim.token_limit         = 8192;
  sim.extra_fields        = [
    { name: 'emotion',     default: 'neutral' },
    { name: 'action',      default: 'speak'   },
    { name: 'action_note', default: ''        },
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

export function applyScenario(s) {
  const cfg = s.config;
  sim.currentScenarioId   = s.id;
  sim.currentScenarioName = s.name;
  sim.agents       = (cfg.agents || []).map(a => ({ ...a, groups: a.groups || [] }));
  sim.background   = cfg.background   || '';
  sim.start_agent  = cfg.start_agent  || (cfg.agents?.[0]?.name ?? '');
  sim.max_waves    = cfg.max_waves    || 10;
  sim.step_delay   = cfg.step_delay   || 1.0;
  // Migrate legacy memory_limit (message count) to token_limit.
  // Old default was 20 messages; ~400 tokens/message is a reasonable estimate.
  sim.token_limit  = cfg.token_limit ?? (cfg.memory_limit ? cfg.memory_limit * 400 : 8192);
  sim.extra_fields = cfg.extra_fields || [
    { name: 'emotion',     default: 'neutral' },
    { name: 'action',      default: 'speak'   },
    { name: 'action_note', default: ''        },
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
