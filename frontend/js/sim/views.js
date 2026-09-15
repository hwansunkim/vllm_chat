// frontend/js/sim/views.js
// View transitions between chat / simulation run / simulation settings.

import { sim } from './state.js';
import { renderAgentCards, getCurrentCardLocations } from './run/cards.js';
import { initD3Graph } from './graph/d3.js';
import { initLocationMap, ensureLocationMap } from './map/d3.js';
import { loadScenarios } from './scenarios.js';
import { renderSettingsPage } from './settings/page.js';

export function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  updateScenarioLabel();
  // 채팅 탭 등 다른 화면에 갔다가 돌아올 때도 같은 이유로 실제 위치를 넘긴다
  // (hideSettingsView() 참고) — 최초 진입(아직 아무 위치도 안 알려진 상태)에는
  // getCurrentCardLocations()가 빈 객체를 주므로 안전하게 설정값으로 시작한다.
  const knownLocations = getCurrentCardLocations();
  renderAgentCards(knownLocations);
  initD3Graph();
  initLocationMap(knownLocations);
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
  // 설정을 열었다 닫을 때마다 카드를 다시 그리는데, 그냥 두면 renderAgentCards()가
  // 시나리오 설정의 초기 위치로 되돌려버린다 — "시뮬레이션을 중지하고 설정을
  // 수정한 뒤 이어서 하기"를 하면 실제로는 백엔드 상태(위치 포함)가 멀쩡히
  // 이어지는데도 화면만 초기 위치로 리셋된 것처럼 보이는 원인이었다. 다시 그리기
  // *직전*의 실제 위치를 떠서 그대로 되돌려준다 — 이름이 안 바뀐 기존 에이전트는
  // 실제 위치를 유지하고, 설정에서 새로 추가/개명된 에이전트만 자연히 설정값으로
  // 시작한다(override에 없으므로).
  const knownLocations = getCurrentCardLocations();
  renderAgentCards(knownLocations);
  // 지도 탭이 열려 있던 채로 설정에서 장소를 편집하고 돌아온 경우, switchTab('map')이
  // 다시 불리지 않아 이전 location_graph로 그려진 지도가 그대로 남는다. 지문이 같으면
  // no-op이라 안 열려 있던 경우엔 비용이 없다. 장소 자체를 편집해 다시 그려야 할
  // 때도(시그니처 변경) 카드와 같은 이유로 실제 위치를 넘긴다.
  ensureLocationMap(knownLocations);
}

export function updateScenarioLabel() {
  const el = document.getElementById('sim-scenario-label');
  if (el) el.textContent = sim.currentScenarioName || '시나리오 없음';
}
