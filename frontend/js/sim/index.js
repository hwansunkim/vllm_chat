// frontend/js/sim/index.js
// Entry point — wires DOM events to the appropriate submodules.

import { sim, _expandedAgents } from './state.js';
import {
  showSimView, hideSimView,
  showSettingsView, hideSettingsView,
} from './views.js';
import { renderOutputFields } from './settings/output-fields.js';
import {
  renderAgentListInConfig,
  renderStartAgentSelect,
} from './settings/agents.js';
import { renderScenarioEvents } from './settings/events.js';
import {
  startSimulation, stopSimulation, continueSimulation,
} from './run/control.js';
import {
  saveScenario, deleteScenario,
  newScenario, applyScenario,
} from './scenarios.js';
import { addLocationNode, initEarlyStopToggle } from './settings/page.js';
import { toggleRunHistory, openAllRunsModal } from './runs/history.js';
import { exportGraph } from './graph/d3.js';
import { switchTab, fetchAgentContext } from './context.js';
import { openExportModal, initExportModal } from './export/markdown.js';
import { initResizeHandles } from './resize.js';

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
  document.getElementById('sim-export-md-btn').addEventListener('click', openExportModal);
  initExportModal();
  initEarlyStopToggle();

  document.getElementById('sim-add-agent-btn').addEventListener('click', () => {
    const newName = `agent${sim.agents.length + 1}`;
    sim.agents.push({ name: newName, display_name: '', icon: '🤖', system_prompt: '', initial_active: true, groups: [] });
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

  document.getElementById('sim-add-location-btn').addEventListener('click', addLocationNode);

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
