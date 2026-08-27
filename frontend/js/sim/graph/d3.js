// frontend/js/sim/graph/d3.js
// D3 force-directed graph for the simulation right panel.
// Module-private state — only the explicit exports cross the boundary.

import { sim, emotionColor, infectionBadge } from '../state.js';

let _d3Sim  = null;
let _d3Data = { nodes: [], links: [], nodeMap: {}, linkMap: {}, maxCount: 0 };

// 길이/접선 측정 전용 <path>. defs 안에 두어 렌더링되지 않는다.
let _probePath = null;

// 발화 빈도 → 엣지 두께 정규화 범위(px).
// 카운트 1 = 기존 고정 두께(1.8px), 현재 그래프의 최댓값 카운트 = MAX.
const LINK_W_MIN = 1.8;
const LINK_W_MAX = 6.5;

// ── 테이퍼(쐐기) 렌더링 파라미터 ──────────────────────────────────────────
// 엣지는 균일 두께 stroke가 아니라 source에서 두껍고 target으로 갈수록 얇아지는
// 채워진 폴리곤으로 그린다. 뾰족한 끝이 곧 방향 표시라 별도 화살표 마커가 없다.
const ARC_BOW       = 1.3;   // linkPath()의 반지름 배율 — 해석적 샘플러와 동일해야 함
const TAPER_STEPS   = 18;    // 폭 오프셋 샘플 수(구간 수). 곡률 대비 충분히 매끄럽다.
const TAPER_TIP_W   = 0.4;   // target 쪽 끝 폭(px) — 0에 가깝게 수렴시켜 뾰족하게
const TAPER_HEAD_K  = 1.5;   // source 쪽 폭 배율. 테이퍼는 평균 폭이 절반이라
                             // 같은 카운트에서 기존 stroke와 시각적 무게를 맞춘다.
const SRC_GAP       = 22;    // source 노드 원(r=22) 경계에서 시작
const TGT_GAP       = 24;    // target 노드 앞에서 종료 (구 marker refX=24와 동일 여유)
const MIN_SPAN       = 6;    // 노드가 가까워 gap 합이 거리를 넘어서도 최소 이만큼은 그린다
const LABEL_GAP      = 6;    // 라벨을 현(chord)에서 호가 휜 방향으로 더 띄우는 거리(px)

/** (source, target) 방향 쌍 키. forceLink가 문자열 id를 노드 객체로 바꾼 뒤에도 동작해야 한다. */
function edgeKey(source, target) {
  const s = (source && typeof source === 'object') ? source.id : source;
  const t = (target && typeof target === 'object') ? target.id : target;
  return `${s}->${t}`;
}

/**
 * 최댓값 카운트 기준 상대 정규화(sqrt 스케일).
 * 소수의 쌍만 반복 발화하는 쏠린 분포에서도 나머지 엣지가 최소 두께로 뭉개지지
 * 않도록 선형 대신 sqrt를 쓴다. 최댓값 쌍이 항상 LINK_W_MAX가 된다.
 */
function linkWidth(count) {
  const c   = Math.max(1, count || 1);
  const max = Math.max(1, _d3Data.maxCount || 1);
  if (max <= 1) return LINK_W_MIN;
  const t = (Math.sqrt(c) - 1) / (Math.sqrt(max) - 1);
  return LINK_W_MIN + (LINK_W_MAX - LINK_W_MIN) * t;
}

function linkTitle(d) {
  const s = (d.source && typeof d.source === 'object') ? d.source.id : d.source;
  const t = (d.target && typeof d.target === 'object') ? d.target.id : d.target;
  return `${s} → ${t} · ${d.emotion} · ${d.count}회`;
}

export function initD3Graph() {
  _d3Data = { nodes: [], links: [], nodeMap: {}, linkMap: {}, maxCount: 0 };
  const svgEl = document.getElementById('sim-graph-svg');
  const svg   = d3.select(svgEl);
  svg.selectAll('*').remove();

  const W = svgEl.clientWidth  || 280;
  const H = svgEl.clientHeight || 400;

  // 화살표 마커 없음 — 테이퍼 폴리곤의 뾰족한 끝이 방향을 표시한다.
  const defs = svg.append('defs');
  _probePath = defs.append('path').attr('class', 'g-spine-probe').node();

  const g = svg.append('g');
  // 링크가 항상 노드 아래에 깔리도록 레이어 분리(새 엣지가 나중에 append돼도 유지).
  const layers = {
    links:  g.append('g').attr('class', 'g-layer-links'),
    labels: g.append('g').attr('class', 'g-layer-labels'),
    nodes:  g.append('g').attr('class', 'g-layer-nodes'),
  };

  svg.call(d3.zoom().scaleExtent([0.3, 4])
    .on('zoom', e => g.attr('transform', e.transform)));

  _d3Sim = d3.forceSimulation([])
    .force('link',      d3.forceLink([]).id(d => d.id).distance(110))
    .force('charge',    d3.forceManyBody().strength(-220))
    .force('center',    d3.forceCenter(W / 2, H / 2))
    .force('collision', d3.forceCollide(36));

  _d3Sim.on('tick', () => {
    g.selectAll('.g-link').attr('d', linkTaperPath);
    g.selectAll('.g-link-label')
      .attr('x', d => labelPos(d).x)
      .attr('y', d => labelPos(d).y);
    g.selectAll('.g-node').attr('transform', d => `translate(${d.x},${d.y})`);
  });

  svg.datum({ g, layers, W, H });
}

/** 엣지의 중심선(spine) — 폴리곤 오프셋의 기준이 되는 호. */
function linkPath(d) {
  const dx = d.target.x - d.source.x;
  const dy = d.target.y - d.source.y;
  const dr = Math.sqrt(dx * dx + dy * dy) * ARC_BOW;
  return `M${d.source.x},${d.source.y}A${dr},${dr} 0 0,1 ${d.target.x},${d.target.y}`;
}

// ── spine 샘플링 ─────────────────────────────────────────────────────────
// 1순위는 DOM 지오메트리 API(getTotalLength/getPointAtLength). 곡선 종류가 바뀌어도
// 그대로 동작한다. probe를 못 쓰는 환경(헤드리스 테스트 등)에서는 동일한 호를
// 해석적으로 계산하는 폴백을 쓴다 — 두 경로 모두 같은 taperPolygon()에 들어간다.

/**
 * 노드가 가까워 SRC_GAP+TGT_GAP이 전체 길이를 넘어서면, 두 gap을 비례 축소해서
 * 최소 MIN_SPAN(px)은 항상 보존한다. 이게 없으면 두 조건이 겹쳐서 나쁘게 나타난다:
 * (a) span이 0 이하가 되는 순간 엣지가 아무 표시 없이 통째로 사라지고,
 * (b) 사라지기 직전 구간(span이 몇 px 남은 경우)에서는 폭(head 최대 9.7px)이 축방향
 *     길이보다 커져서 방향을 읽을 수 없는 뭉툭한 덩어리가 된다.
 * 완전히 겹치는(len <= MIN_SPAN) 경우에만 진짜로 포기(null)한다.
 */
function clampGaps(len, gapStart, gapEnd) {
  const total = gapStart + gapEnd;
  if (total <= 0 || len - total > MIN_SPAN) return [gapStart, gapEnd];
  const k = Math.max(0, len - MIN_SPAN) / total;
  return [gapStart * k, gapEnd * k];
}

/** 양끝을 각각 gapStart/gapEnd(px)만큼 잘라낸 뒤 steps+1개 점을 등간격 샘플. */
function sampleSpineDom(dStr, steps, gapStart, gapEnd) {
  if (!_probePath || typeof _probePath.getTotalLength !== 'function') return null;
  let len;
  try {
    _probePath.setAttribute('d', dStr);
    len = _probePath.getTotalLength();
  } catch { return null; }
  if (!Number.isFinite(len) || len <= 0) return null;

  const [gs, ge] = clampGaps(len, gapStart, gapEnd);
  const span = len - gs - ge;
  if (span <= 0) return null;                      // 완전히 겹칠 때만 그리지 않음

  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const p = _probePath.getPointAtLength(gs + (i / steps) * span);
    pts.push({ x: p.x, y: p.y });
  }
  return pts;
}

/**
 * linkPath()가 만드는 호를 해석적으로 샘플링(폴백/테스트용).
 * `A dr,dr 0 0,1` = 원호, large-arc=0 / sweep=1 → 중심 오프셋 부호는 +.
 * 원호는 각도 등간격 = 호길이 등간격이라 gap도 각도(gap/r)로 환산한다.
 */
export function sampleSpineArc(x0, y0, x1, y1, steps, gapStart = 0, gapEnd = 0) {
  const dx = x1 - x0, dy = y1 - y0;
  const dist = Math.hypot(dx, dy);
  if (!(dist > 0)) return null;

  const r  = dist * ARC_BOW;
  const hx = dx / 2, hy = dy / 2;
  const h2 = hx * hx + hy * hy;
  const k  = Math.sqrt(Math.max(0, (r * r - h2) / h2));
  const cx = -k * hy + (x0 + x1) / 2;
  const cy =  k * hx + (y0 + y1) / 2;

  const th0 = Math.atan2(y0 - cy, x0 - cx);
  let delta = Math.atan2(y1 - cy, x1 - cx) - th0;
  if (delta < 0) delta += Math.PI * 2;            // sweep=1 → 각도 증가 방향

  const len = r * delta;
  const [gs, ge] = clampGaps(len, gapStart, gapEnd);
  const span = len - gs - ge;
  if (span <= 0) return null;                     // 완전히 겹칠 때만 그리지 않음

  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const th = th0 + (gs + (i / steps) * span) / r;
    pts.push({ x: cx + r * Math.cos(th), y: cy + r * Math.sin(th) });
  }
  return pts;
}

const rnd = n => Math.round(n * 100) / 100;

/**
 * 샘플 점열을 폭 wStart→wEnd로 선형 보간해 좌우로 오프셋한 폐곡선 폴리곤 d 문자열.
 * 각 점의 법선은 이웃 샘플 차분 벡터를 90도 회전해 근사한다.
 * `+`쪽을 source→target으로, `-`쪽을 target→source로 이어 붙여 자기교차 없는 쐐기를 만든다.
 */
export function taperPolygon(pts, wStart, wEnd) {
  const n = pts && pts.length;
  if (!n || n < 2) return '';

  const plus = [], minus = [];
  for (let i = 0; i < n; i++) {
    // 폭을 선형이 아니라 t^2(ease-in)로 보간한다 — count=1처럼 wStart/wEnd 차이가
    // 작을 때도 자루 구간은 오래 두껍게 유지하다 끝에서 빠르게 좁아져 뾰족함이
    // 뚜렷해진다. t가 여전히 0→1 단조 증가라 자기교차 성질은 그대로 유지된다.
    const t = Math.pow(i / (n - 1), 2);
    const h = (wStart + (wEnd - wStart) * t) / 2;   // 반폭
    const a = pts[Math.max(0, i - 1)];
    const b = pts[Math.min(n - 1, i + 1)];
    let tx = b.x - a.x, ty = b.y - a.y;
    const m = Math.hypot(tx, ty);
    if (m < 1e-9) { tx = 1; ty = 0; } else { tx /= m; ty /= m; }
    const nx = -ty, ny = tx;                        // 접선 90도 회전 = 법선
    plus.push([pts[i].x + nx * h, pts[i].y + ny * h]);
    minus.push([pts[i].x - nx * h, pts[i].y - ny * h]);
  }

  const seg = p => `${rnd(p[0])},${rnd(p[1])}`;
  let d = `M${seg(plus[0])}`;
  for (let i = 1; i < n; i++) d += `L${seg(plus[i])}`;
  for (let i = n - 1; i >= 0; i--) d += `L${seg(minus[i])}`;
  return d + 'Z';
}

/**
 * 엣지 하나의 렌더링용 d — source에서 두껍고 target에서 뾰족한 채움 폴리곤.
 * 해석적 샘플러(sampleSpineArc)를 1순위로 쓴다 — DOM probe(setAttribute +
 * getTotalLength/getPointAtLength, 엣지·tick마다 19회)는 Blink가 매번 arc-length
 * table을 재구축해 엣지 30개 안팎부터 16ms 프레임 예산을 넘긴다. 해석적 샘플러는
 * 같은 호(linkPath)를 삼각함수로 직접 계산해 오차 0.002px 이내로 동일한 점을
 * 내면서 DOM 왕복이 없다. probe는 이제 폴백(예: 곡선 종류가 바뀌어 해석식이
 * 더 이상 안 맞게 되는 경우)으로만 남긴다.
 */
function linkTaperPath(d) {
  const pts = sampleSpineArc(d.source.x, d.source.y, d.target.x, d.target.y,
                             TAPER_STEPS, SRC_GAP, TGT_GAP)
           ?? sampleSpineDom(linkPath(d), TAPER_STEPS, SRC_GAP, TGT_GAP);
  if (!pts) return '';
  return taperPolygon(pts, linkWidth(d.count) * TAPER_HEAD_K, TAPER_TIP_W);
}

/**
 * 링크 라벨 위치 — 현(chord) 중점이 아니라 호(spine)가 실제로 휜 지점을 쓴다.
 * A→B와 B→A는 반대 방향으로 휘므로(동일 sweep 규칙, 진행 방향이 반대) 각자
 * 다른 쪽으로 자연히 떨어진다. 거기서 한 번 더 같은 방향으로 LABEL_GAP만큼 밀어
 * 텍스트가 폴리곤 채움 위에 바로 겹치지 않고 살짝 바깥으로 뜨게 한다.
 */
export function labelPos(d) {
  const cx = (d.source.x + d.target.x) / 2, cy = (d.source.y + d.target.y) / 2;
  const pts = sampleSpineArc(d.source.x, d.source.y, d.target.x, d.target.y, 2, 0, 0);
  const mid = pts ? pts[1] : { x: cx, y: cy };
  let ox = mid.x - cx, oy = mid.y - cy;
  const m = Math.hypot(ox, oy);
  if (m > 1e-6) { ox /= m; oy /= m; } else { ox = 0; oy = -1; }
  return { x: mid.x + ox * LABEL_GAP, y: mid.y + oy * LABEL_GAP };
}

export function addD3Edge(source, target, emotion) {
  if (!_d3Sim) return;
  const svg    = d3.select('#sim-graph-svg');
  const layers = svg.datum()?.layers;
  if (!layers) return;

  [source, target].forEach(name => {
    if (name === 'self' || name === 'system' || name === 'all' || _d3Data.nodeMap[name]) return;
    const agent = sim.agents.find(a => a.name === name) || { icon: '🤖', name };
    const node  = { id: name, icon: agent.icon };
    _d3Data.nodes.push(node);
    _d3Data.nodeMap[name] = node;
  });

  if (target !== 'self' && target !== 'system' && target !== 'all') {
    // 방향 쌍(A→B)을 키로 누적 — A→B와 B→A는 병합하지 않고 별개 엣지로 남는다.
    const key      = edgeKey(source, target);
    const existing = _d3Data.linkMap[key];
    if (existing) {
      existing.count  += 1;
      existing.emotion = emotion || 'neutral';   // 색은 가장 최근 발화 감정 기준
      if (existing.count > _d3Data.maxCount) _d3Data.maxCount = existing.count;
    } else {
      const link = { source, target, emotion: emotion || 'neutral', count: 1 };
      _d3Data.links.push(link);
      _d3Data.linkMap[key] = link;
      if (_d3Data.maxCount < 1) _d3Data.maxCount = 1;
    }
  }

  _d3Sim.nodes(_d3Data.nodes);
  _d3Sim.force('link').links(_d3Data.links);

  // 상대 정규화라 최댓값이 바뀌면 기존 엣지 두께도 전부 달라진다.
  // 폭은 tick마다 d(폴리곤)를 다시 만들면서 반영되고, 여기서는 색/툴팁을 전량 재적용한다.
  const linkSel   = layers.links.selectAll('.g-link').data(_d3Data.links, d => edgeKey(d.source, d.target));
  linkSel.exit().remove();
  const linkEnter = linkSel.enter().append('path').attr('class', 'g-link').attr('stroke', 'none');
  linkEnter.append('title');
  linkEnter.merge(linkSel)
    .attr('fill', d => emotionColor(d.emotion))
    .attr('d', linkTaperPath)
    .select('title').text(linkTitle);

  const lblSel = layers.labels.selectAll('.g-link-label').data(_d3Data.links, d => edgeKey(d.source, d.target));
  lblSel.exit().remove();
  lblSel.enter().append('text').attr('class', 'g-link-label')
    .merge(lblSel).text(d => d.emotion);

  const nodeSel   = layers.nodes.selectAll('.g-node').data(_d3Data.nodes, d => d.id);
  const nodeEnter = nodeSel.enter().append('g').attr('class', 'g-node')
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) _d3Sim.alphaTarget(0); d.fx = null; d.fy = null; })
    );

  nodeEnter.append('circle').attr('r', 22).attr('fill', '#eef2ff');
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', -4)
    .attr('font-size', '17px').text(d => d.icon);
  nodeEnter.append('text').attr('text-anchor', 'middle').attr('y', 14)
    .attr('font-size', '10px').attr('fill', '#475569').text(d => d.id);

  // 엣지가 생기며 노드가 나중에 추가될 수도 있으므로, 감염 상태는 매번 전량 재적용한다.
  refreshInfectionStyles();

  _d3Sim.alpha(0.4).restart();
}

/**
 * 감염 상태(sim.agentInfection)를 노드 테두리 색으로 반영한다.
 * infection_update가 올 때와 노드가 새로 생길 때 양쪽에서 호출된다 —
 * 상태는 전부 sim.agentInfection 한 곳에서 읽으므로 순서에 의존하지 않는다.
 */
export function refreshInfectionStyles() {
  if (typeof d3 === 'undefined') return;
  const svg = d3.select('#sim-graph-svg');
  const layers = svg.datum()?.layers;
  if (!layers) return;
  layers.nodes.selectAll('.g-node').each(function (d) {
    const rec   = sim.agentInfection?.[d.id];
    const badge = rec ? infectionBadge(rec.status, rec.cause) : null;
    const sel   = d3.select(this);
    sel.classed('g-node-infected',  badge?.cls === 'infected');
    sel.classed('g-node-recovered', badge?.cls === 'recovered');
  });
}

export function exportGraph() {
  const svgEl = document.getElementById('sim-graph-svg');
  const blob  = new Blob([new XMLSerializer().serializeToString(svgEl)], { type: 'image/svg+xml' });
  const url   = URL.createObjectURL(blob);
  const a     = Object.assign(document.createElement('a'), { href: url, download: 'sim-graph.svg' });
  a.click();
  URL.revokeObjectURL(url);
}

/** Test/debug accessor — returns the live d3 force simulation. */
export function getD3Sim() {
  return _d3Sim;
}
