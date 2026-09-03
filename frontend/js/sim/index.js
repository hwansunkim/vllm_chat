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
import { initErrorPopupEvents } from './run/errors.js';
import {
  saveScenario, deleteScenario,
  newScenario, applyScenario,
  exportScenarioFile, importScenarioFile,
} from './scenarios.js';
import { addLocationNode, addTimeCategory, initEarlyStopToggle, initTimeModeToggle, initTargetDurationUI,
         addSymptomStage } from './settings/page.js';
import { toggleRunHistory, openAllRunsModal } from './runs/history.js';
import { exportGraph } from './graph/d3.js';
import { exportLocationMap } from './map/d3.js';
import { switchTab, fetchAgentContext } from './context.js';
import { openExportModal, initExportModal } from './export/markdown.js';
import { initResizeHandles } from './resize.js';
import { initImportChatAgentEvents } from './settings/import-chat-agent.js';
import { initSettingsSections } from './settings/sections.js';
import { initContractPreview } from './settings/contract-preview.js';
import { initAutoGrow, initTextEditorOverlay } from './settings/textareas.js';

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
  document.getElementById('sim-export-scenario-btn').addEventListener('click', exportScenarioFile);
  document.getElementById('sim-import-scenario-btn').addEventListener('click', () => {
    document.getElementById('sim-import-scenario-input').click();
  });
  document.getElementById('sim-import-scenario-input').addEventListener('change', async e => {
    const file = e.target.files[0];
    if (file) await importScenarioFile(file);
    e.target.value = '';
  });
  document.getElementById('sim-history-btn')?.addEventListener('click', toggleRunHistory);
  document.getElementById('sim-all-runs-btn').addEventListener('click', openAllRunsModal);
  // "⬇ SVG"는 그래프/지도 탭에서 공용 — 현재 보이는 pane 기준으로 내보낸다.
  document.getElementById('sim-export-graph-btn').addEventListener('click', () => {
    const mapPane = document.getElementById('sim-tab-map');
    if (mapPane && !mapPane.classList.contains('sim-hidden')) exportLocationMap();
    else exportGraph();
  });
  document.getElementById('sim-export-md-btn').addEventListener('click', openExportModal);
  initExportModal();
  initErrorPopupEvents();
  initEarlyStopToggle();
  initTimeModeToggle();
  initTargetDurationUI();

  // 설정 페이지 레이아웃 — 섹션 아코디언 + 네비 레일, auto-grow textarea, 확대 오버레이.
  initSettingsSections();
  initAutoGrow();
  initTextEditorOverlay();

  // "💬 채팅에서 가져오기" — 에이전트 카드 편집기 섹션 헤더의 진입점.
  initImportChatAgentEvents();

  document.getElementById('sim-add-agent-btn').addEventListener('click', () => {
    const newName = `agent${sim.agents.length + 1}`;
    sim.agents.push({ name: newName, display_name: '', icon: '🤖', system_prompt: '', initial_active: true, groups: [], server_id: null, temperature: null, relationships: {} });
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
  document.getElementById('sim-inf-add-stage-btn')?.addEventListener('click', addSymptomStage);

  document.getElementById('sim-add-timecat-btn')?.addEventListener('click', addTimeCategory);

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

  // 계약 미리보기 + 출력 계약 오버라이드.
  // 예전에 여기 있던 `GET /api/simulation/default-output-format` 부팅 fetch 는 제거했다 —
  // 그 엔드포인트는 이제 항상 빈 문자열을 돌려주고(계약 프리즈 중단), 기본 계약은
  // 엔진이 실행 시점에 만든다. 사용자가 볼 것은 "그때 무엇이 만들어지는가"(미리보기)다.
  initContractPreview();
}
