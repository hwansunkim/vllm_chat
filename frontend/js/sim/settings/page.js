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
  document.getElementById('sim-output-format').value  = sim.output_format_template || '';
  const delBtn = document.getElementById('sim-delete-scenario-btn');
  if (delBtn) delBtn.disabled = !sim.currentScenarioId;
  renderOutputFields();
  renderAgentListInConfig();
  renderStartAgentSelect();
  renderScenarioEvents();
}

export function readConfigFromUI() {
  sim.background             = document.getElementById('sim-background').value.trim();
  sim.start_agent            = document.getElementById('sim-start-agent').value;
  sim.max_waves              = parseInt(document.getElementById('sim-max-waves').value)    || 10;
  sim.step_delay             = parseFloat(document.getElementById('sim-step-delay').value) || 1.0;
  sim.token_limit            = parseInt(document.getElementById('sim-token-limit').value)  || 8192;
  sim.output_format_template = document.getElementById('sim-output-format').value;
}
