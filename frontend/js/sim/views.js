// frontend/js/sim/views.js
// View transitions between chat / simulation run / simulation settings.

import { sim } from './state.js';
import { renderAgentCards, getCurrentCardLocations, syncAgentRoster } from './run/cards.js';
import { ensureD3Graph } from './graph/d3.js';
import { ensureLocationMap } from './map/d3.js';
import { loadScenarios } from './scenarios.js';
import { renderSettingsPage } from './settings/page.js';

export function showSimView() {
  document.getElementById('main').classList.add('sim-hidden');
  document.getElementById('sim-settings-view').classList.add('sim-hidden');
  document.getElementById('sim-view').classList.remove('sim-hidden');
  updateScenarioLabel();
  // 화면 복귀는 "다시 그리기"일 뿐 실행 교체가 아니다 — 실행 상태(감염·수면/이동·감정·
  // 만남·발언·토큰·등퇴장)는 비우지 않고 renderAgentCards()가 새 카드에 다시 칠한다.
  // 초기화는 새 실행/이력 불러오기 경로(resetRunState())에서만 한다.
  // 위치는 카드가 이미 알고 있는 실제 위치를 넘긴다 — 최초 진입(아직 아무 위치도 안
  // 알려진 상태)에는 빈 객체라 설정값으로 시작한다.
  syncAgentRoster();   // 바뀐 에이전트의 위치가 스냅샷에 섞이지 않도록 먼저 정리
  const knownLocations = getCurrentCardLocations();
  renderAgentCards(knownLocations);
  // 그래프·지도도 지우지 않는다: 그래프는 아직 없을 때만 만들고, 지도는 장소·에이전트
  // 구성이 바뀐 경우에만 다시 세운다(그때도 실제 위치·진행 중 만남은 유지).
  ensureD3Graph();
  ensureLocationMap(knownLocations);
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
  // 설정을 열었다 닫을 때마다 카드를 다시 그린다. 실행 상태(감염·상태 뱃지·감정·만남 등)는
  // renderAgentCards()가 비우지 않고 다시 칠하며, 설정에서 삭제·개명된 에이전트 것만 버린다.
  // 위치는 override로 넘기는데, 예전에는 이걸 안 하면 renderAgentCards()가
  // 시나리오 설정의 초기 위치로 되돌려버린다 — "시뮬레이션을 중지하고 설정을
  // 수정한 뒤 이어서 하기"를 하면 실제로는 백엔드 상태(위치 포함)가 멀쩡히
  // 이어지는데도 화면만 초기 위치로 리셋된 것처럼 보이는 원인이었다. 다시 그리기
  // *직전*의 실제 위치를 떠서 그대로 되돌려준다 — 이름이 안 바뀐 기존 에이전트는
  // 실제 위치를 유지하고, 설정에서 새로 추가/개명된 에이전트만 자연히 설정값으로
  // 시작한다(override에 없으므로).
  syncAgentRoster();   // 바뀐 에이전트의 위치가 스냅샷에 섞이지 않도록 먼저 정리
  const knownLocations = getCurrentCardLocations();
  renderAgentCards(knownLocations);
  // 지도 탭이 열려 있던 채로 설정에서 장소(또는 에이전트 구성)를 편집하고 돌아온 경우, switchTab('map')이
  // 다시 불리지 않아 이전 location_graph로 그려진 지도가 그대로 남는다. 지문이 같으면
  // no-op이라 안 열려 있던 경우엔 비용이 없다. 장소 자체를 편집해 다시 그려야 할
  // 때도(시그니처 변경) 카드와 같은 이유로 실제 위치를 넘긴다.
  ensureLocationMap(knownLocations);
}

export function updateScenarioLabel() {
  const el = document.getElementById('sim-scenario-label');
  if (el) el.textContent = sim.currentScenarioName || '시나리오 없음';
}
