// frontend/js/sim/views.js
// View transitions between chat / simulation run / simulation settings.

import { sim } from './state.js';
import { renderAgentCards } from './run/cards.js';
import { initD3Graph } from './graph/d3.js';
import { initLocationMap, ensureLocationMap } from './map/d3.js';
import { loadScenarios } from './scenarios.js';
import { renderSettingsPage } from './settings/page.js';

export function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  updateScenarioLabel();
  renderAgentCards();
  initD3Graph();
  initLocationMap();
  loadScenarios();
}

export function hideSimView() {
  document.getElementById('sim-view').classList.add('sim-hidden');
  document.getElementById('main').classList.remove('sim-hidden');
}

export function showSettingsView() {
  document.getElementById('sim-view').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.remove('sim-hidden');
  renderSettingsPage();
  loadScenarios();
}

export function hideSettingsView() {
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  updateScenarioLabel();
  renderAgentCards();
  // 지도 탭이 열려 있던 채로 설정에서 장소를 편집하고 돌아온 경우, switchTab('map')이
  // 다시 불리지 않아 이전 location_graph로 그려진 지도가 그대로 남는다. 지문이 같으면
  // no-op이라 안 열려 있던 경우엔 비용이 없다.
  ensureLocationMap();
}

export function updateScenarioLabel() {
  const el = document.getElementById('sim-scenario-label');
  if (el) el.textContent = sim.currentScenarioName || '시나리오 없음';
}
