// frontend/js/sim/settings/location-graph.js
// 위치 그래프 에디터 — 장소 노드/연결/zone 편집과 sim.location_graph 동기화.

import { sim, esc } from '../state.js';
import { updateSectionBadges } from './sections.js';

export function renderLocationGraph() {
  const container = document.getElementById('sim-location-graph');
  if (!container) return;
  const graph = sim.location_graph || [];
  const nodeNames = graph.map(n => n.name);
  container.innerHTML = '';
  updateSectionBadges(sim);   // 장소 개수가 섹션 헤더 뱃지에 그대로 노출된다

  // zone 자유 텍스트의 오타로 "우리집"/"우리 집"이 서로 다른 zone으로 갈라지면 인지 기능이
  // 조용히 깨진다. 이미 쓰인 zone을 datalist로 제안해 표기를 통일시킨다.
  const zoneList = document.createElement('datalist');
  zoneList.id = 'sim-loc-zone-list';
  container.appendChild(zoneList);

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
        <input class="sim-loc-node-zone" data-idx="${idx}" list="sim-loc-zone-list"
               value="${esc(node.zone || '')}" placeholder="zone(선택)"
               title="구역 — 같은 zone의 장소에 있는 에이전트끼리는 서로의 존재를 인지합니다(대화는 같은 장소에서만 가능). 비워두면 zone 없음." />
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

  syncZoneDatalist();

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
    const zoneInput = e.target.closest('.sim-loc-node-zone');
    if (zoneInput) {
      const i = parseInt(zoneInput.dataset.idx);
      const zone = zoneInput.value.trim();
      zoneInput.value = zone;
      sim.location_graph[i].zone = zone;
      // 재렌더 없이 datalist만 갱신한다 — change는 blur 시점에 발생하므로 여기서
      // renderLocationGraph()를 부르면 다음 요소로 넘어가던 탭 포커스가 사라진다.
      syncZoneDatalist();
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

// 현재 그래프에 쓰인 zone 값들을 zone 입력의 자동완성 목록으로 채운다.
function syncZoneDatalist() {
  const dl = document.getElementById('sim-loc-zone-list');
  if (!dl) return;
  const zones = [...new Set(
    (sim.location_graph || []).map(n => (n.zone || '').trim()).filter(Boolean)
  )].sort();
  dl.innerHTML = '';
  zones.forEach(z => {
    const opt = document.createElement('option');
    opt.value = z;   // 속성 대입이라 이스케이프 불필요
    dl.appendChild(opt);
  });
}

export function readLocationGraph() {
  return (sim.location_graph || []).map((n, idx) => {
    // zone 입력은 change(=blur) 시점에 모델로 반영된다. 포커스가 남아 있는 채로
    // 설정을 떠나는 경로를 대비해 DOM 값이 있으면 그쪽을 우선한다. 패널이 렌더되지
    // 않은 상태로 불릴 수도 있으므로 없을 때만 기존 상태로 폴백한다.
    const zoneEl = document.querySelector(`#sim-location-graph .sim-loc-node-zone[data-idx="${idx}"]`);
    return {
      name:        n.name,
      connects_to: [...(n.connects_to || [])],
      is_exterior: !!n.is_exterior,
      zone:        (zoneEl ? zoneEl.value : (n.zone || '')).trim(),
    };
  });
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [], zone: '' });
  renderLocationGraph();
}
