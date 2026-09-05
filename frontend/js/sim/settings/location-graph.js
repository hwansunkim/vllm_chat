// frontend/js/sim/settings/location-graph.js
// 위치 그래프 에디터 — 장소 노드/연결/zone 편집과 sim.location_graph 동기화.
// 같은 섹션에 사는 공간 기반 인지(perception_mode) 토글도 여기서 다룬다 — 그 규칙이
// 방(node)과 구역(zone)이라는 이 그래프의 개념 위에서만 의미를 갖기 때문이다.

import { sim, esc } from '../state.js';
import { updateSectionBadges } from './sections.js';

// ── 공간 기반 인지 (perception_mode) ─────────────────────────────────────────
// 'targeted'(기본) = 발화가 target에 지목된 상대에게만 전달된다(기존 동작 100% 유지).
// 'spatial'        = ① 같은 장소의 제3자가 지목되지 않아도 엿듣고,
//                    ② 같은 zone의 다른 장소에 있는 지목 상대(<key>/stranger_N)에게는
//                       행동 묘사 없이 대사만 전달되고("[화자, 멀리서] …"),
//                       ③ 혼잣말은 대사가 새지 않고 행동만 같은 장소 사람들에게 보인다.
// UI는 체크박스 하나지만 백엔드 계약은 문자열이므로 경계에서만 변환한다.

export function normalizePerceptionMode(v) {
  return v === 'spatial' ? 'spatial' : 'targeted';
}

// 체크박스가 DOM에 없는 경로(설정 패널을 한 번도 열지 않고 저장/시작 등)에서는
// 기존 상태 → 기본값 순으로 폴백한다 — time_estimation_mode와 같은 규칙.
export function readPerceptionMode() {
  const chk = document.getElementById('sim-perception-spatial');
  if (!chk) return normalizePerceptionMode(sim.perception_mode);
  return chk.checked ? 'spatial' : 'targeted';
}

/** 상태 → 폼. renderSettingsPage()가 호출한다. */
export function renderPerceptionMode() {
  const chk = document.getElementById('sim-perception-spatial');
  if (chk) chk.checked = normalizePerceptionMode(sim.perception_mode) === 'spatial';
}

export function initPerceptionModeToggle() {
  const chk = document.getElementById('sim-perception-spatial');
  if (!chk) return;
  chk.onchange = () => {
    sim.perception_mode = chk.checked ? 'spatial' : 'targeted';
    updateSectionBadges(sim);   // 접힌 world 섹션 헤더/네비 뱃지("엿듣기")를 즉시 갱신
  };
}

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
    const myZone = (node.zone || '').trim();
    const connBadges = (node.connects_to || []).map(c => {
      const isZoneConn = !nodeNames.includes(c);   // 노드명 우선 — 같은 이름의 노드가 없으면 zone 참조로 본다
      return `
      <span class="sim-loc-conn-badge${isZoneConn ? ' sim-loc-conn-zone' : ''}">
        ${isZoneConn ? '🏠 ' : ''}${esc(c)}
        <button class="sim-loc-conn-del" data-node="${idx}" data-conn="${esc(c)}"
                data-zone="${isZoneConn ? '1' : '0'}" title="연결 제거">×</button>
      </span>`;
    }).join('');
    const otherNodes = nodeNames.filter(n => n !== node.name && !(node.connects_to || []).includes(n));
    const addConnOpts = otherNodes.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    // 입구가 지정된 zone만 연결 대상이 된다. 이 노드 자신의 zone과 이미 연결된 zone은 제외.
    const entryZones = [...new Set(
      graph.filter(n => n.is_zone_entry && (n.zone || '').trim()).map(n => (n.zone || '').trim())
    )].filter(z => z !== myZone && !(node.connects_to || []).includes(z));
    const zoneConnOpts = entryZones.map(z => `<option value="${esc(z)}" data-zone="1">🏠 ${esc(z)}</option>`).join('');
    const canEntry = !!myZone;
    row.innerHTML = `
      <div class="sim-loc-node-header">
        <span class="sim-loc-node-icon">${isExt ? '🌐' : '📍'}</span>
        <input class="sim-loc-node-name" data-idx="${idx}" value="${esc(node.name)}" placeholder="장소 이름" />
        <label class="sim-loc-exterior-toggle" title="외부 공간 — 이 장소에 있는 에이전트는 서로를 볼 수 없고 내부와 소통 불가">
          <input type="checkbox" class="sim-loc-exterior-chk" data-idx="${idx}" ${isExt ? 'checked' : ''}>
          <span>외부</span>
        </label>
        <label class="sim-loc-entry-toggle${canEntry ? '' : ' sim-loc-entry-disabled'}"
               title="구역 입구 — 바깥에서 이 구역으로 들어올 때 거치는 장소. 구역당 하나만 지정할 수 있으며, zone을 먼저 입력해야 합니다.">
          <input type="checkbox" class="sim-loc-entry-chk" data-idx="${idx}" ${node.is_zone_entry && canEntry ? 'checked' : ''} ${canEntry ? '' : 'disabled'}>
          <span>입구</span>
        </label>
        <input class="sim-loc-node-zone" data-idx="${idx}" list="sim-loc-zone-list"
               value="${esc(node.zone || '')}" placeholder="zone(선택)"
               title="구역 — 같은 zone의 장소에 있는 에이전트끼리는 서로의 존재를 인지합니다(대화는 같은 장소에서만 가능). 비워두면 zone 없음." />
        <button class="sim-loc-node-del" data-idx="${idx}" title="장소 삭제">×</button>
      </div>
      <div class="sim-loc-conns">
        <span class="sim-loc-conns-label">연결:</span>
        ${connBadges || '<span class="sim-loc-no-conn">(없음)</span>'}
        ${(addConnOpts || zoneConnOpts) ? `<select class="sim-loc-add-conn" data-node="${idx}">
          <option value="">+ 연결 추가</option>
          ${addConnOpts ? `<optgroup label="장소">${addConnOpts}</optgroup>` : ''}
          ${zoneConnOpts ? `<optgroup label="구역">${zoneConnOpts}</optgroup>` : ''}
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
      // zone 연결은 단방향(엔진이 진입/탈출로 전개) — 역방향 엣지가 없으므로 정리 스킵.
      if (delConn.dataset.zone !== '1') {
        const other = sim.location_graph.find(n => n.name === conn);
        if (other) other.connects_to = (other.connects_to || []).filter(c => c !== sim.location_graph[ni].name);
      }
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
    const entryChk = e.target.closest('.sim-loc-entry-chk');
    if (entryChk) {
      const i = parseInt(entryChk.dataset.idx);
      const node = sim.location_graph[i];
      const zone = (node.zone || '').trim();
      if (entryChk.checked && zone) {
        // zone당 입구는 하나 — 같은 zone의 다른 노드 입구 지정을 자동 해제한다.
        sim.location_graph.forEach((n, j) => {
          if (j !== i && (n.zone || '').trim() === zone) n.is_zone_entry = false;
        });
        node.is_zone_entry = true;
      } else {
        node.is_zone_entry = false;
      }
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
      const oldZone = (sim.location_graph[i].zone || '').trim();
      zoneInput.value = zone;
      sim.location_graph[i].zone = zone;
      // zone 이름이 바뀌면 이 노드를 참조하던 다른 노드의 connects_to zone 참조도
      // 따라 바꾼다 — 노드 이름 변경(위)과 같은 이유. 안 하면 street.connects_to=["집"]가
      // 조용히 미해결 참조가 된다. 옛 zone명이 아직 다른 노드에 살아있으면 건드리지 않는다.
      if (oldZone && zone && oldZone !== zone) {
        const oldZoneStillUsed = sim.location_graph.some(
          (n, j) => j !== i && (n.zone || '').trim() === oldZone
        );
        if (!oldZoneStillUsed) {
          sim.location_graph.forEach(n => {
            n.connects_to = (n.connects_to || []).map(c => c === oldZone ? zone : c);
          });
        }
      }
      // zone을 비우면 "입구" 지정도 의미가 없으므로 함께 해제한다.
      if (!zone) sim.location_graph[i].is_zone_entry = false;
      // 재렌더 없이 입구 토글의 활성 상태만 DOM에서 맞춘다 — change는 blur 시점에
      // 발생하므로 여기서 renderLocationGraph()를 부르면 탭 포커스가 사라진다.
      const entryEl = container.querySelector(`.sim-loc-entry-chk[data-idx="${i}"]`);
      if (entryEl) {
        entryEl.disabled = !zone;
        if (!zone) entryEl.checked = false;
        entryEl.closest('.sim-loc-entry-toggle')?.classList.toggle('sim-loc-entry-disabled', !zone);
      }
      syncZoneDatalist();
      return;
    }
    const addConn = e.target.closest('.sim-loc-add-conn');
    if (addConn) {
      const ni = parseInt(addConn.dataset.node);
      const target = addConn.value;
      if (!target) return;
      const isZoneConn = addConn.selectedOptions[0]?.dataset.zone === '1';
      const nodeA = sim.location_graph[ni];
      if (!nodeA.connects_to) nodeA.connects_to = [];
      if (!nodeA.connects_to.includes(target)) nodeA.connects_to.push(target);
      // zone 연결은 단방향으로만 저장한다 — 엔진이 진입(X→입구)/탈출(내부→X)로
      // 비대칭 전개하므로 프론트에서 역방향 엣지를 만들면 안 된다.
      if (!isZoneConn) {
        const nodeB = sim.location_graph.find(n => n.name === target);
        if (nodeB) {
          if (!nodeB.connects_to) nodeB.connects_to = [];
          if (!nodeB.connects_to.includes(nodeA.name)) nodeB.connects_to.push(nodeA.name);
        }
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
    const zone = (zoneEl ? zoneEl.value : (n.zone || '')).trim();
    return {
      name:          n.name,
      connects_to:   [...(n.connects_to || [])],
      is_exterior:   !!n.is_exterior,
      zone,
      // zone이 없으면 입구 개념도 없다 — 엔진도 zone 없는 is_zone_entry는 무시(warning).
      is_zone_entry: !!n.is_zone_entry && !!zone,
    };
  });
}

// 위치 그래프 "+ 장소 추가" 버튼
export function addLocationNode() {
  if (!sim.location_graph) sim.location_graph = [];
  sim.location_graph.push({ name: `장소${sim.location_graph.length + 1}`, connects_to: [], zone: '', is_zone_entry: false });
  renderLocationGraph();
}
