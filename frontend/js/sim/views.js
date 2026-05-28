// frontend/js/sim/views.js
// View transitions between chat / simulation run / simulation settings.

import { sim } from './state.js';
import { renderAgentCards } from './run/cards.js';
import { initD3Graph } from './graph/d3.js';
import { loadScenarios } from './scenarios.js';
import { renderSettingsPage } from './settings/page.js';

export function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  updateScenarioLabel();
  renderAgentCards();
  initD3Graph();
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
}

export function updateScenarioLabel() {
  const el = document.getElementById('sim-scenario-label');
  if (el) el.textContent = sim.currentScenarioName || '시나리오 없음';
}
