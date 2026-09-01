// frontend/js/sim/map/d3.js
// 위치 지도 (오른쪽 패널 "위치 지도" 탭).
//   장소       = 넓은 박스(사각형)
//   connects_to = 박스와 박스를 잇는 복도(무방향 선)
//   에이전트    = 박스 안에 놓이는 원형 아바타(이모지 + 이름)
//
// 데이터는 전부 로컬(sim.location_graph, sim.agents, agent_move SSE)에서 온다 —
// 별도 API 호출 없음. 좌표 스키마가 없으므로 박스 배치는 d3-force로 자동 계산하며,
// 관계 그래프(graph/d3.js)와는 완전히 독립된 별도 simulation 인스턴스를 쓴다.

import { sim, getAgentIcon, infectionBadge } from '../state.js';

// ── 레이아웃 상수 ─────────────────────────────────────────────────────────────
const BASE_R    = 15;    // 아바타 기본 반지름 (관계 그래프 r=22보다 작다 — 박스 안에 여럿 들어가야 함)
const MIN_R     = 6;     // 아무리 붐벼도 이 아래로는 줄이지 않는다
const CELL_K    = 0.92;  // 셀 대비 아바타 크기 배율. 1보다 크게 잡히면 서로 겹친다(허용)
const TITLE_H   = 18;    // 박스 상단 제목 밴드 — 아바타 배치 영역에서 제외
const PAD       = 5;     // 박스 안쪽 여백
const BOX_MIN_W = 92,  BOX_MAX_W = 200;
const BOX_MIN_H = 62,  BOX_MAX_H = 170;
const NAME_FS   = 8;     // 아바타 이름 폰트 크기(px)
const TITLE_FS  = 9.5;   // 박스 제목 폰트 크기(px)
const LABEL_DY  = 9;     // 원 아래 이름 baseline 오프셋
const LABEL_MIN_R = 9;   // 이보다 작아지면 이름을 생략(이모지만) — 박스 밖으로 새는 것 방지

// ── 추격선(meeting_update) 상수 ──────────────────────────────────────────────
// chaser 아바타 → target 아바타를 잇는 점선 화살표. 아바타 원 안에서 시작/끝나지 않도록
// 양 끝을 반지름 + 여유만큼 물려 놓고, 그러고도 남는 길이가 없으면(거의 겹쳐 있으면)
// 아예 그리지 않는다 — 동석한 두 사람 사이에 점 하나짜리 선이 남는 걸 막는다.
const CHASE_GAP_FROM = 3;    // chaser 원 바깥 여유(px)
const CHASE_GAP_TO   = 7;    // target 원 바깥 여유(px) — 화살촉 자리
const CHASE_MIN_LEN  = 6;    // 이보다 짧아지면 생략

// ── zone(구역) 시각화 상수 ───────────────────────────────────────────────────
// zone은 "같은 zone의 다른 장소에 있는 사람끼리 서로 존재를 인지"하는 논리적 묶음이라
// 복도(link)로는 이어지지 않을 수 있다. 그래서 (a) 약한 클러스터링 force로 화면상
// 가깝게 모으고, (b) 박스들을 감싸는 반투명 배경(halo)으로 묶여 있음을 보여준다.
const ZONE_PAD         = 24;   // 박스 바깥으로 halo가 나가는 여유(px)
const ZONE_FALLBACK_R  = 18;   // 1~2개짜리 zone 폴백 둥근사각형의 모서리 반경
const ZONE_LABEL_INSET = 15;   // halo 위쪽 가장자리에서 라벨 baseline까지(px)
const ZONE_FS          = 10.5; // zone 이름 폰트 크기(px)
const ZONE_STRENGTH    = 0.07; // zone 구심력 — link(0.55) 레이아웃과 싸우지 않을 만큼만 약하게
const ZONE_REPEL_STRENGTH = 0.10; // zone 소속이 아닌 노드를 그 zone 영역 밖으로 미는 척력
const ZONE_REPEL_MARGIN   = 12;   // 척력이 시작되는 지점 = zone 점유 반경 + ZONE_PAD + 이 값
// zone 개수는 가변이므로 고정 키 매핑(관계 그래프의 EMOTION_COLORS)을 쓸 수 없다.
// 정렬된 zone 이름 목록의 인덱스를 이 배열에 순환 대입한다(같은 구성이면 항상 같은 색).
const ZONE_COLORS = [
  '#6366f1', '#f59e0b', '#10b981', '#ec4899', '#0ea5e9',
  '#a855f7', '#ef4444', '#14b8a6', '#84cc16', '#f97316',
];

// ── 모듈 전역 상태 ────────────────────────────────────────────────────────────
let _mapSim   = null;
let _mapData  = { nodes: [], links: [], nodeMap: {}, zones: [] };
let _zoneLine = null;    // d3.line(curveCatmullRomClosed) — d3 로드 후 지연 생성
let _agentLoc = {};      // agentName -> location name (SSE agent_move로 갱신)
// chaser agentName -> { target, targetName, targetLocation } (SSE meeting_update로 갱신).
// 만남 lock이 살아있는 동안만 항목이 존재하며, 비어 있으면 추격선 렌더는 즉시 반환한다.
let _meetingIntent = {};
let _agents   = [];      // 렌더링용 아바타 데이터 (recomputeAgents가 매번 재생성)
let _layers   = null;
let _zoom     = null;
let _gRoot    = null;
let _userZoomed = false; // 사용자가 직접 줌/팬하면 자동 fit을 멈춘다
let _W = 0, _H = 0;
let _signature = null;   // location_graph 구조 지문 — 설정 변경 감지용

// ── 작은 헬퍼 ────────────────────────────────────────────────────────────────
const r2 = n => Math.round(n * 100) / 100;

const WIDE_CH = /[ᄀ-ᇿ⺀-꓏가-힣豈-﫿︰-﹏＀-｠]/;

// 이모지(Extended_Pictographic)도 CJK와 비슷하게 1em 폭을 쓴다 — 0.58em으로 잡으면
// 이모지로만 된 이름이 박스 폭을 넘어 새어나간다.
const EMOJI_CH = /\p{Extended_Pictographic}/u;
// 폭 추정은 근사치라 실측(getBBox 등)보다 좁게 잡힐 수 있어, 클램프에는 안전
// 마진을 살짝 얹어 경계값에서도 실제로 안 새게 한다.
const WIDTH_SAFETY = 1.08;

/** 텍스트 픽셀 폭 근사치. CJK/이모지는 1em, 그 외는 0.58em으로 잡는다(박스 안 클램프용). */
function estTextW(s, fs) {
  let w = 0;
  for (const ch of String(s ?? '')) {
    w += (WIDE_CH.test(ch) || EMOJI_CH.test(ch)) ? fs : fs * 0.58;
  }
  return w * WIDTH_SAFETY;
}

function shortLabel(s, maxChars) {
  // 코드포인트 단위로 자른다 — length/slice(UTF-16 코드유닛 기준)를 쓰면 이모지
  // 대리쌍(surrogate pair)이 중간에서 잘려 깨진 글자가 나온다.
  const cp = [...String(s ?? '')];
  if (maxChars < 1) return '';
  return cp.length <= maxChars ? cp.join('') : `${cp.slice(0, Math.max(1, maxChars - 1)).join('')}…`;
}

const halfDiag = n => Math.hypot(n.w, n.h) / 2;

const clamp = (v, lo, hi) => (lo > hi ? (lo + hi) / 2 : Math.min(hi, Math.max(lo, v)));

/**
 * location_graph 구조 지문 — 설정 화면에서 장소가 바뀌었는지 싸게 감지한다.
 * zone도 반드시 포함해야 한다 — 빠지면 "zone만 바꾸고 지도 탭으로 돌아온" 경우
 * ensureLocationMap이 변경을 감지하지 못해 zone 배경이 옛 구성으로 남는다.
 */
function graphSignature() {
  return JSON.stringify((sim.location_graph || []).map(n => [
    n?.name ?? '', [...(n?.connects_to || [])].sort(), !!n?.is_exterior,
    (n?.zone ?? '').trim(),
  ]));
}

// ── 박스 크기 산정 ───────────────────────────────────────────────────────────
/**
 * 박스 크기는 init 시점에 한 번만 정한다(이후 고정).
 * - 가로: 장소 이름이 잘리지 않을 만큼
 * - 세로/가로: "평균 수용 인원(capacity)"이 BASE_R 크기로 들어갈 만큼
 * 실행 중 특정 장소에 인원이 몰리는 건 박스를 키우는 대신 아바타를 줄여서 흡수한다.
 * (박스 크기가 매 이동마다 변하면 force 레이아웃 전체가 출렁이기 때문)
 */
function boxSizeFor(name, capacity) {
  const cols = Math.max(1, Math.ceil(Math.sqrt(capacity)));
  const rows = Math.max(1, Math.ceil(capacity / cols));
  const nameW  = estTextW(`📍 ${name}`, TITLE_FS) + 26;
  const needW  = cols * BASE_R * 2 * CELL_K + PAD * 2;
  // 라벨 높이(LABEL_DY + 텍스트 높이)는 CELL_K로 줄이지 않는다 — 원끼리는 겹쳐도
  // 되지만 라벨은 줄어들지 않으므로, 여기서 같이 줄이면 실제 필요 높이를 과소
  // 계상해 layoutBoxAgents의 세로 클램프가 항상 라벨 높이만큼 헐거워진다(H-1).
  const needH  = rows * (BASE_R * 2 * CELL_K + LABEL_DY + NAME_FS * 0.35) + TITLE_H + PAD * 2;
  return {
    w: clamp(Math.max(nameW, needW), BOX_MIN_W, BOX_MAX_W),
    h: clamp(needH, BOX_MIN_H, BOX_MAX_H),
  };
}

// ── 데이터 빌드 ──────────────────────────────────────────────────────────────
function buildData(graph) {
  const nodes = [], nodeMap = {};
  const capacity = Math.max(2, Math.ceil((sim.agents?.length || 0) / Math.max(1, graph.length)) + 1);

  graph.forEach(loc => {
    const name = loc?.name;
    if (!name || nodeMap[name]) return;          // 이름 없음/중복은 건너뛴다
    const { w, h } = boxSizeFor(name, capacity);
    const node = {
      id: name, name, exterior: !!loc.is_exterior, w, h, count: 0,
      zone: (loc.zone || '').trim(),   // '' = zone 없음 → 어떤 배경 영역에도 안 들어간다
    };
    nodes.push(node);
    nodeMap[name] = node;
  });

  // connects_to는 양방향이라 A→B / B→A가 둘 다 들어온다 — 정렬 키로 중복 제거.
  const links = [], seen = new Set();
  graph.forEach(loc => {
    const from = loc?.name;
    if (!from || !nodeMap[from]) return;
    (loc.connects_to || []).forEach(to => {
      if (!to || to === from || !nodeMap[to]) return;
      const key = [from, to].sort().join(' ');
      if (seen.has(key)) return;
      seen.add(key);
      links.push({ source: from, target: to });
    });
  });

  return { nodes, links, nodeMap, zones: buildZones(nodes) };
}

/**
 * zone별 묶음 + 색 배정.
 * 색은 "정렬된 zone 이름 목록의 인덱스 % 팔레트 길이" — 같은 zone 구성이면 재초기화
 * 후에도 항상 같은 색이 나오고, 팔레트보다 zone이 많으면 순환한다.
 * zone이 빈 문자열인 장소는 아예 대상에서 빠진다.
 */
function buildZones(nodes) {
  const byZone = new Map();
  for (const n of nodes) {
    if (!n.zone) continue;
    if (!byZone.has(n.zone)) byZone.set(n.zone, []);
    byZone.get(n.zone).push(n);
  }
  return [...byZone.keys()].sort().map((key, i) => ({
    key,
    members: byZone.get(key),
    color: ZONE_COLORS[i % ZONE_COLORS.length],
  }));
}

// ── 에이전트 배치 (박스 경계 내부 보장) ────────────────────────────────────────
/**
 * 한 박스 안의 에이전트들을 격자로 배치한다.
 * 1) 박스 안쪽 영역(제목 밴드 제외)의 종횡비에 맞춰 열/행 수를 정하고
 * 2) 셀 크기에서 아바타 반지름을 역산 — 셀보다 살짝 크게(CELL_K) 잡아 겹침을 허용하며
 * 3) 마지막에 좌표를 박스 안쪽으로 클램프해 "절대 박스 밖으로 안 나감"을 보장한다.
 * 이름 텍스트도 폭을 추정해 클램프 반경에 포함하므로 라벨이 벽을 넘지 않는다.
 */
function layoutBoxAgents(node, list) {
  const n = list.length;
  if (!n) return;

  const innerW = Math.max(1, node.w - PAD * 2);
  const innerH = Math.max(1, node.h - TITLE_H - PAD * 2);

  let cols = Math.round(Math.sqrt((n * innerW) / innerH));
  cols = Math.max(1, Math.min(n, cols || 1));
  const rows  = Math.ceil(n / cols);
  const cellW = innerW / cols;
  const cellH = innerH / rows;

  // 셀의 절반보다 CELL_K배 크게 → 이웃과 살짝 겹치되 전체는 박스 안에 남는다.
  const LABEL_EXT = LABEL_DY + NAME_FS * 0.35;   // 원 아래로 라벨이 차지하는 높이
  let r = Math.max(
    MIN_R,
    Math.min(BASE_R, (cellW / 2) / CELL_K, (cellH / 2) / CELL_K),
  );
  let showLabel = r >= LABEL_MIN_R;
  if (showLabel) {
    // 라벨을 보여줄 거면, 셀 높이에서 라벨 몫을 뺀 나머지로 반지름을 다시 잡는다.
    // 그래야 "반지름 r + 라벨 높이"가 셀을 넘지 않아 클램프가 실제로 안전해진다.
    r = Math.max(MIN_R, Math.min(r, ((cellH - LABEL_EXT) / 2) / CELL_K));
    showLabel = r >= LABEL_MIN_R;
  }
  const maxChars  = Math.max(0, Math.floor((node.w - PAD * 2) / NAME_FS));

  const left = -node.w / 2 + PAD;
  const top  = -node.h / 2 + TITLE_H + PAD;

  list.forEach((agent, i) => {
    const row = Math.floor(i / cols);
    const col = i % cols;
    // 마지막 행이 덜 찼으면 가운데 정렬해서 어색한 왼쪽 쏠림을 없앤다.
    const inRow  = Math.min(cols, n - row * cols);
    const rowOff = (cols - inRow) * cellW / 2;

    const label = agent.display_name || agent.name;
    const short = showLabel ? shortLabel(label, maxChars) : '';
    const halfW = Math.max(r, estTextW(short, NAME_FS) / 2);
    // 라벨 baseline은 원 중심이 아니라 dy + r + LABEL_DY(렌더러 :384 `d.r + LABEL_DY`)에
    // 찍히므로, 하단 클램프 여유는 r 자체도 포함해야 한다 — 빠지면 라벨이 항상
    // r px만큼 박스 밖으로 샌다(H-1).
    const botExt = showLabel ? r + LABEL_EXT : r;

    const rawX = left + rowOff + (col + 0.5) * cellW;
    const rawY = top + (row + 0.5) * cellH;

    _agents.push({
      name:  agent.name,
      label,
      short,
      showLabel,
      r,
      icon: getAgentIcon(agent, sim.agentEmotions?.[agent.name]),
      loc:  node.name,
      node,
      // 박스 경계 클램프 — 여기서 나가는 좌표는 정의상 박스 안이다.
      dx: clamp(rawX, -node.w / 2 + PAD + halfW,  node.w / 2 - PAD - halfW),
      dy: clamp(rawY, -node.h / 2 + TITLE_H + PAD + r, node.h / 2 - PAD - botExt),
    });
  });
}

function recomputeAgents() {
  _agents = [];
  const byLoc = {};
  const unplaced = [];

  for (const a of (sim.agents || [])) {
    const loc = _agentLoc[a.name];
    if (!loc || !_mapData.nodeMap[loc]) { unplaced.push(a.display_name || a.name); continue; }
    (byLoc[loc] = byLoc[loc] || []).push(a);
  }

  for (const node of _mapData.nodes) {
    const list = byLoc[node.name] || [];
    node.count = list.length;
    layoutBoxAgents(node, list);
  }

  updateHint(unplaced);
}

function updateHint(unplaced) {
  const el = document.getElementById('sim-map-hint');
  if (!el) return;
  if (!unplaced.length) { el.classList.add('sim-hidden'); el.textContent = ''; return; }
  const shown = unplaced.slice(0, 4).join(', ');
  el.textContent = `위치 미지정: ${shown}${unplaced.length > 4 ? ` 외 ${unplaced.length - 4}명` : ''}`;
  el.classList.remove('sim-hidden');
}

// ── 복도(링크) 경로 ──────────────────────────────────────────────────────────
/** 박스 중심에서 (dx,dy) 방향으로 나갈 때 만나는 테두리 위의 점. */
function boxEdgePoint(n, dx, dy) {
  const ax = Math.abs(dx), ay = Math.abs(dy);
  if (ax < 1e-6 && ay < 1e-6) return { x: n.x, y: n.y };
  const t = Math.min(ax > 1e-6 ? (n.w / 2) / ax : Infinity,
                     ay > 1e-6 ? (n.h / 2) / ay : Infinity);
  return { x: n.x + dx * t, y: n.y + dy * t };
}

/** 무방향 복도 — 두 박스 테두리를 잇는 직선(화살표/테이퍼 없음). */
function corridorPath(d) {
  const s = d.source, t = d.target;
  if (!s || !t || s.x == null || t.x == null) return '';
  const p0 = boxEdgePoint(s, t.x - s.x, t.y - s.y);
  const p1 = boxEdgePoint(t, s.x - t.x, s.y - t.y);
  return `M${r2(p0.x)},${r2(p0.y)}L${r2(p1.x)},${r2(p1.y)}`;
}

// ── zone 배경(halo) 기하 ─────────────────────────────────────────────────────
/** 박스의 실제 사각형 네 모서리. 중심점만 감싸면 박스 모서리가 halo 밖으로 삐져나온다. */
function boxCorners(n) {
  const hw = n.w / 2, hh = n.h / 2;
  return [
    [n.x - hw, n.y - hh], [n.x + hw, n.y - hh],
    [n.x + hw, n.y + hh], [n.x - hw, n.y + hh],
  ];
}

/** 볼록 다각형을 중심에서 바깥으로 pad만큼 밀어 여유를 준다. */
function padPolygon(poly, pad) {
  let cx = 0, cy = 0;
  for (const p of poly) { cx += p[0]; cy += p[1]; }
  cx /= poly.length; cy /= poly.length;
  return poly.map(([x, y]) => {
    const dx = x - cx, dy = y - cy;
    const len = Math.hypot(dx, dy) || 1;
    return [x + (dx / len) * pad, y + (dy / len) * pad];
  });
}

function roundRectPath(x0, y0, x1, y1, r) {
  const rr = Math.max(0, Math.min(r, (x1 - x0) / 2, (y1 - y0) / 2));
  const [a, b, c, d, e] = [r2(x0), r2(y0), r2(x1), r2(y1), r2(rr)];
  return `M${r2(x0 + rr)},${b}H${r2(x1 - rr)}A${e},${e} 0 0 1 ${c},${r2(y0 + rr)}`
       + `V${r2(y1 - rr)}A${e},${e} 0 0 1 ${r2(x1 - rr)},${d}`
       + `H${r2(x0 + rr)}A${e},${e} 0 0 1 ${a},${r2(y1 - rr)}`
       + `V${r2(y0 + rr)}A${e},${e} 0 0 1 ${r2(x0 + rr)},${b}Z`;
}

/**
 * zone 영역의 path + 라벨 배치용 다각형을 계산한다.
 * - 박스 3개 이상: 모든 박스 모서리의 convex hull → 패딩 → 둥근(catmull-rom) 폐곡선
 * - 박스 1~2개: hull이 사실상 사각형/막대라 곡선을 씌우면 어색하므로
 *   bounding box + 패딩의 둥근사각형으로 대체한다(hull 폴백).
 */
function zoneShape(z) {
  const members = z.members.filter(n => n.x != null && n.y != null);
  if (!members.length) return null;

  if (members.length >= 3 && typeof d3 !== 'undefined' && d3.polygonHull) {
    const pts = [];
    for (const n of members) pts.push(...boxCorners(n));
    const hull = d3.polygonHull(pts);
    if (hull && hull.length >= 3) {
      const poly = padPolygon(hull, ZONE_PAD);
      _zoneLine = _zoneLine || d3.line().curve(d3.curveCatmullRomClosed.alpha(0.5));
      return { d: _zoneLine(poly), poly };
    }
  }

  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of members) {
    x0 = Math.min(x0, n.x - n.w / 2); x1 = Math.max(x1, n.x + n.w / 2);
    y0 = Math.min(y0, n.y - n.h / 2); y1 = Math.max(y1, n.y + n.h / 2);
  }
  x0 -= ZONE_PAD; y0 -= ZONE_PAD; x1 += ZONE_PAD; y1 += ZONE_PAD;
  return {
    d: roundRectPath(x0, y0, x1, y1, ZONE_FALLBACK_R),
    poly: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
  };
}

/**
 * 라벨 위치 — 영역 위쪽 가장자리 바로 안쪽.
 * y = (최상단 + INSET) 수평선이 다각형을 자르는 구간의 중점을 쓴다. 볼록 다각형이므로
 * 이 점은 항상 영역 안이고, 실제로 그려지는 곡선은 다각형보다 살짝 부풀기 때문에
 * 곡선 기준으로도 안쪽에 들어온다.
 */
function zoneLabelPos(poly) {
  let minY = Infinity, cx = 0, cy = 0;
  for (const p of poly) { minY = Math.min(minY, p[1]); cx += p[0]; cy += p[1]; }
  cx /= poly.length; cy /= poly.length;

  const y = Math.min(minY + ZONE_LABEL_INSET, cy);
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < poly.length; i++) {
    const a = poly[i], b = poly[(i + 1) % poly.length];
    if ((a[1] - y) * (b[1] - y) > 0) continue;                 // 같은 쪽 → 교차 없음
    if (Math.abs(b[1] - a[1]) < 1e-6) {                        // 수평 변 → 양 끝 사용
      lo = Math.min(lo, a[0], b[0]); hi = Math.max(hi, a[0], b[0]);
      continue;
    }
    const x = a[0] + (b[0] - a[0]) * ((y - a[1]) / (b[1] - a[1]));
    lo = Math.min(lo, x); hi = Math.max(hi, x);
  }
  return Number.isFinite(lo) ? { x: (lo + hi) / 2, y } : { x: cx, y: cy };
}

/**
 * zone 클러스터링 force — 두 가지 일을 한다.
 *   1) 인력: 같은 zone 박스를 그 zone의 (매 tick 재계산되는) 중심으로 살짝 당긴다.
 *      복도 연결이 없어도 같은 zone이 화면상 뭉쳐 보이게 하는 게 목적.
 *   2) 척력: 그 zone 소속이 아닌 노드가 zone의 "점유 반경"(멤버들이 실제로 중심에서
 *      얼마나 퍼져 있는지, 박스 크기 포함) + 여유 안쪽까지 들어오면 바깥으로 민다.
 *      인력만 있으면 "뭉치기"는 되지만 "안 겹치기"는 보장이 안 돼서, 무소속/다른 zone
 *      노드가 halo 안쪽까지 들어와 버리는 문제가 있었다(리뷰에서 발견). 척력은 그
 *      확률을 줄여줄 뿐 0으로 만들진 못하므로, 박스 자신의 zone-색 테두리를 안전장치로
 *      같이 둔다.
 * 둘 다 link(0.55)/charge(-620) 레이아웃을 이길 만큼 세면 안 된다(ZONE_STRENGTH,
 * ZONE_REPEL_STRENGTH 참고). 혼자뿐인 zone은 자기 자신이 곧 중심이라 인력은 무효과이고,
 * 점유 반경도 박스 자기 반지름 정도로만 잡혀 척력 범위가 과하게 넓어지지 않는다.
 */
function zoneClusterForce(strength, repelStrength) {
  let nodes = [];
  function force(alpha) {
    const cent = new Map();
    for (const n of nodes) {
      if (!n.zone) continue;
      let c = cent.get(n.zone);
      if (!c) cent.set(n.zone, (c = { x: 0, y: 0, n: 0 }));
      c.x += n.x; c.y += n.y; c.n++;
    }
    for (const c of cent.values()) { c.x /= c.n; c.y /= c.n; }

    // zone별 점유 반경 — 그 zone 멤버 중 중심에서 가장 멀리 있는 박스까지의 거리
    // (박스 절반 크기 포함). 척력이 미치는 범위를 zone 크기에 맞춘다.
    const radius = new Map();
    for (const n of nodes) {
      if (!n.zone) continue;
      const c = cent.get(n.zone);
      if (!c) continue;
      const half = Math.hypot(n.w, n.h) / 2;
      const d = Math.hypot(n.x - c.x, n.y - c.y) + half;
      radius.set(n.zone, Math.max(radius.get(n.zone) || 0, d));
    }

    const k = strength * alpha;
    for (const n of nodes) {
      if (!n.zone) continue;
      const c = cent.get(n.zone);
      if (!c || c.n < 2) continue;
      n.vx += (c.x - n.x) * k;
      n.vy += (c.y - n.y) * k;
    }

    const rk = repelStrength * alpha;
    for (const [zone, c] of cent) {
      const reach = (radius.get(zone) || 0) + ZONE_PAD + ZONE_REPEL_MARGIN;
      for (const n of nodes) {
        if (n.zone === zone) continue;  // 자기 zone은 인력 쪽이 담당, 척력 대상에서 제외
        const dx = n.x - c.x, dy = n.y - c.y;
        const dist = Math.hypot(dx, dy) || 1;
        if (dist >= reach) continue;
        // 깊이 들어와 있을수록(경계 근처보다) 세게 민다.
        const push = (reach - dist) * rk;
        n.vx += (dx / dist) * push;
        n.vy += (dy / dist) * push;
      }
    }
  }
  force.initialize = _nodes => { nodes = _nodes; };
  return force;
}

// ── 렌더링 ───────────────────────────────────────────────────────────────────
/**
 * 지도를 처음부터 다시 그린다.
 * 시뮬레이션 시작 / 뷰 진입 / 리플레이 로드 시점에 호출 (관계 그래프의 initD3Graph와 같은 지점).
 */
export function initLocationMap() {
  const graph = Array.isArray(sim.location_graph) ? sim.location_graph : [];
  _signature = graphSignature();
  _mapData   = buildData(graph);

  // 초기 위치는 에이전트 설정의 location. 그래프에 없는 값은 미배치로 둔다.
  _agentLoc = {};
  _meetingIntent = {};   // 새 실행 = 진행 중인 만남 없음
  for (const a of (sim.agents || [])) {
    if (a.location && _mapData.nodeMap[a.location]) _agentLoc[a.name] = a.location;
  }

  _userZoomed = false;
  _W = _H = 0;
  renderMap();
  updateTabState();
}

function updateTabState() {
  const btn = document.querySelector('.sim-tab[data-tab="map"]');
  if (!btn) return;
  const n = _mapData.nodes.length;
  btn.classList.toggle('sim-tab-dim', n === 0);
  btn.title = n === 0
    ? '이 시나리오에는 위치 그래프가 설정되지 않았습니다'
    : `${n}개 장소`;
}

function renderMap() {
  const svgEl = document.getElementById('sim-map-svg');
  if (!svgEl || typeof d3 === 'undefined') return;

  if (_mapSim) { _mapSim.stop(); _mapSim = null; }
  _layers = _gRoot = _zoom = null;
  _agents = [];

  const svg = d3.select(svgEl);
  svg.on('.zoom', null);
  svg.selectAll('*').remove();

  const empty = _mapData.nodes.length === 0;
  document.getElementById('sim-map-empty')?.classList.toggle('sim-hidden', !empty);
  svgEl.classList.toggle('sim-hidden', empty);
  if (empty) { updateHint([]); return; }

  // 탭이 숨겨져 있으면 clientWidth가 0 — 폴백으로 그린 뒤 탭 전환 시 refreshMapSize가 재조정한다.
  const W = svgEl.clientWidth  || 280;
  const H = svgEl.clientHeight || 400;
  _W = svgEl.clientWidth || 0;
  _H = svgEl.clientHeight || 0;

  // 추격선 화살촉. marker는 defs 안에 한 번만 두고 id로 참조한다.
  // markerUnits를 userSpaceOnUse로 고정 — 기본값(strokeWidth)이면 CSS에서 선 굵기를
  // 바꿀 때 화살촉 크기까지 같이 변한다.
  svg.append('defs').append('marker')
    .attr('id', 'lm-chase-arrow')
    .attr('viewBox', '0 0 8 8')
    .attr('refX', 7).attr('refY', 4)
    .attr('markerWidth', 8).attr('markerHeight', 8)
    .attr('markerUnits', 'userSpaceOnUse')
    .attr('orient', 'auto')
    .append('path')
    .attr('class', 'lm-chase-head')
    .attr('d', 'M0,0.5L8,4L0,7.5Z');

  _gRoot = svg.append('g');
  // 속성 순서 = append 순서 = z-order. zone 배경은 복도/박스보다 뒤에 깔려야 하므로 맨 처음.
  // 추격선은 박스 위 / 아바타 아래 — 박스를 가로질러 보이되 아바타를 덮지는 않는다.
  _layers = {
    zones:         _gRoot.append('g').attr('class', 'lm-layer-zones'),
    corridorOuter: _gRoot.append('g').attr('class', 'lm-layer-corridor-outer'),
    corridorInner: _gRoot.append('g').attr('class', 'lm-layer-corridor-inner'),
    boxes:         _gRoot.append('g').attr('class', 'lm-layer-boxes'),
    chase:         _gRoot.append('g').attr('class', 'lm-layer-chase'),
    agents:        _gRoot.append('g').attr('class', 'lm-layer-agents'),
  };

  _zoom = d3.zoom().scaleExtent([0.2, 3]).on('zoom', e => {
    _gRoot.attr('transform', e.transform);
    if (e.sourceEvent) _userZoomed = true;   // 프로그램적 fit(sourceEvent 없음)은 제외
  });
  svg.call(_zoom);

  _mapSim = d3.forceSimulation(_mapData.nodes)
    .force('link', d3.forceLink(_mapData.links).id(d => d.id)
      .distance(l => (halfDiag(l.source) + halfDiag(l.target)) * 1.15 + 28)
      .strength(0.55))
    .force('charge',  d3.forceManyBody().strength(-620))
    // 사각형이므로 충돌 반경은 대각선 절반으로 근사(약간 보수적 = 절대 안 겹침).
    .force('collide', d3.forceCollide(d => halfDiag(d) + 8).strength(0.9))
    .force('center',  d3.forceCenter(W / 2, H / 2))
    // 연결이 없는 고립 장소가 charge에 밀려 날아가지 않도록 약한 구심력.
    .force('x', d3.forceX(W / 2).strength(0.045))
    .force('y', d3.forceY(H / 2).strength(0.045))
    // 같은 zone끼리 뭉치게 하는 약한 구심력(위 x/y의 zone 인지 버전).
    .force('zone', zoneClusterForce(ZONE_STRENGTH, ZONE_REPEL_STRENGTH));

  // ── zone 배경 (가장 아래 레이어) ──
  const zone = _layers.zones.selectAll('.lm-zone')
    .data(_mapData.zones, d => d.key).enter().append('g').attr('class', 'lm-zone');
  zone.append('path')
    .attr('class', 'lm-zone-area')
    // fill/stroke는 zone마다 달라 presentation attribute로 넣는다(CSS는 불투명도/선폭만).
    .attr('fill', d => d.color).attr('stroke', d => d.color);
  zone.append('text')
    .attr('class', 'lm-zone-label')
    .attr('text-anchor', 'middle')
    .attr('font-size', `${ZONE_FS}px`)
    .attr('fill', d => d.color)
    .text(d => shortLabel(d.key, 18));
  zone.append('title').text(d => `${d.key} — ${d.members.length}개 장소`);

  // ── 복도 (박스 아래 레이어) ──
  const outer = _layers.corridorOuter.selectAll('.lm-corridor')
    .data(_mapData.links).enter().append('path').attr('class', 'lm-corridor');
  outer.append('title').text(d => {
    const s = typeof d.source === 'object' ? d.source.id : d.source;
    const t = typeof d.target === 'object' ? d.target.id : d.target;
    return `${s} ↔ ${t}`;
  });
  _layers.corridorInner.selectAll('.lm-corridor-in')
    .data(_mapData.links).enter().append('path').attr('class', 'lm-corridor-in');

  // ── 장소 박스 ──
  const box = _layers.boxes.selectAll('.lm-box')
    .data(_mapData.nodes, d => d.id).enter().append('g')
    .attr('class', 'lm-box')
    .call(d3.drag()
      .on('start', (ev, d) => { if (!ev.active) _mapSim.alphaTarget(0.25).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
      .on('end',   (ev, d) => { if (!ev.active) _mapSim.alphaTarget(0); d.fx = null; d.fy = null; }));

  // zone 이름 → 그 zone의 헐로 색. convex hull은 서로 다른 zone의 박스를 시각적으로
  // 감쌀 수 있어서(예: zone 없는 허브 노드가 다른 zone 헐로 안에 놓이는 경우), 헐로
  // 색만으로는 "이 박스가 실제로 어느 zone인지"가 모호해진다. 박스 자신의 테두리를
  // 자기 zone 색으로 칠해서, 어떤 헐로 안에 있든 소속을 즉시 구분할 수 있게 한다.
  const zoneColorOf = new Map(_mapData.zones.map(z => [z.key, z.color]));

  box.append('rect')
    .attr('class', d => `lm-box-rect${d.exterior ? ' lm-exterior' : ''}`)
    .attr('x', d => -d.w / 2).attr('y', d => -d.h / 2)
    .attr('width', d => d.w).attr('height', d => d.h)
    .attr('rx', 9)
    .attr('stroke', d => d.zone ? zoneColorOf.get(d.zone) : null)
    .attr('stroke-width', d => d.zone ? 2.5 : null);
  box.append('text')
    .attr('class', d => `lm-box-title${d.exterior ? ' lm-exterior-title' : ''}`)
    .attr('x', d => -d.w / 2 + PAD + 1)
    .attr('y', d => -d.h / 2 + TITLE_FS + 3)
    .attr('font-size', `${TITLE_FS}px`)
    .text(d => `${d.exterior ? '🌐' : '📍'} ${shortLabel(d.name, Math.floor((d.w - 30) / TITLE_FS))}`);
  box.append('text')
    .attr('class', 'lm-box-count')
    .attr('text-anchor', 'end')
    .attr('x', d => d.w / 2 - PAD)
    .attr('y', d => -d.h / 2 + TITLE_FS + 3);
  box.append('title').text(d => `${d.name}${d.exterior ? ' (외부 공간)' : ''}`);

  _mapSim.on('tick', () => {
    updateZones();
    _layers.corridorOuter.selectAll('.lm-corridor').attr('d', corridorPath);
    _layers.corridorInner.selectAll('.lm-corridor-in').attr('d', corridorPath);
    _layers.boxes.selectAll('.lm-box').attr('transform', d => `translate(${r2(d.x)},${r2(d.y)})`);
    positionAgents(false);
  });
  _mapSim.on('end', fitToView);

  updateZones();
  renderAgents(false);
}

/** zone 배경/라벨을 현재 박스 좌표에 맞춰 다시 그린다 (tick마다 호출). */
function updateZones() {
  if (!_layers) return;
  _layers.zones.selectAll('.lm-zone').each(function (z) {
    const g = d3.select(this);
    const s = zoneShape(z);
    // 아직 좌표가 없는 tick 이전 상태에서는 (0,0) 근처에 잔상이 남지 않도록 숨긴다.
    if (!s || !s.d) { g.attr('display', 'none'); return; }
    const p = zoneLabelPos(s.poly);
    g.attr('display', null);
    g.select('.lm-zone-area').attr('d', s.d);
    g.select('.lm-zone-label').attr('x', r2(p.x)).attr('y', r2(p.y));
  });
}

/** 아바타 데이터 재계산 + DOM 반영. animate=true면 이동을 부드럽게 보간한다. */
function renderAgents(animate) {
  if (!_layers) return;
  recomputeAgents();

  _layers.boxes.selectAll('.lm-box-count').text(d => (d.count ? `👥 ${d.count}` : ''));

  const sel = _layers.agents.selectAll('.lm-agent').data(_agents, d => d.name);
  sel.exit().remove();

  const enter = sel.enter().append('g').attr('class', 'lm-agent');
  enter.append('circle').attr('class', 'lm-agent-dot');
  enter.append('text').attr('class', 'lm-agent-icon').attr('text-anchor', 'middle');
  enter.append('text').attr('class', 'lm-agent-name').attr('text-anchor', 'middle');
  enter.append('title');
  // 새로 들어온 아바타는 (0,0)에서 날아오지 않도록 즉시 제자리에 놓는다.
  enter.attr('transform', d => `translate(${r2(d.node.x || 0)},${r2(d.node.y || 0)})`);

  const all = enter.merge(sel);
  all.select('.lm-agent-dot').attr('r', d => d.r);
  all.select('.lm-agent-icon')
    .attr('y', d => d.r * 0.35)
    .attr('font-size', d => `${Math.max(8, d.r * 0.95)}px`)
    .text(d => d.icon);
  all.select('.lm-agent-name')
    .attr('y', d => d.r + LABEL_DY)
    .attr('font-size', `${NAME_FS}px`)
    .text(d => d.short);
  all.select('title').text(d => `${d.label} · ${d.loc}`);
  _applyInfectionClasses(all);

  positionAgents(animate);
}

/** 아바타 원에 감염 상태 클래스를 적용 (상태는 sim.agentInfection 한 곳에서만 읽는다). */
function _applyInfectionClasses(sel) {
  sel.each(function (d) {
    const rec   = sim.agentInfection?.[d.name];
    const badge = rec ? infectionBadge(rec.status, rec.cause) : null;
    d3.select(this)
      .classed('lm-agent-infected',  badge?.cls === 'infected')
      .classed('lm-agent-recovered', badge?.cls === 'recovered');
  });
}

/**
 * infection_update SSE 훅 — 아바타 테두리를 감염 상태 색으로 갈아끼운다.
 * 지도가 아직 없거나(위치 그래프 미설정) 해당 아바타가 미배치면 조용히 무시한다 —
 * 상태 자체는 sim.agentInfection에 남아 다음 렌더에서 반영된다.
 */
export function updateAgentInfectionOnMap(agentName) {
  if (!_layers || !agentName) return;
  _applyInfectionClasses(_layers.agents.selectAll('.lm-agent').filter(d => d.name === agentName));
}

function positionAgents(animate) {
  if (!_layers) return;
  const tf = d => `translate(${r2((d.node.x || 0) + d.dx)},${r2((d.node.y || 0) + d.dy)})`;
  const sel = _layers.agents.selectAll('.lm-agent');
  if (animate) sel.transition().duration(550).ease(d3.easeCubicInOut).attr('transform', tf);
  else sel.interrupt().attr('transform', tf);
  // 추격선의 양 끝은 아바타 좌표라 아바타가 움직일 때마다 같이 갱신되어야 한다.
  // positionAgents는 tick과 renderAgents(=agent_move) 양쪽에서 불리므로 여기 한 곳이면 충분하다.
  renderChaseLines(animate);
}

// ── 추격선 (meeting_update) ──────────────────────────────────────────────────
/** 아바타의 절대 좌표(박스 중심 + 박스 안 오프셋). */
function agentXY(a) {
  return { x: (a.node.x || 0) + a.dx, y: (a.node.y || 0) + a.dy };
}

/**
 * chaser→target 선분의 실제 끝점. 그릴 수 없으면 null.
 *
 * 같은 장소면 그리지 않는다. 엔진의 lock 해제 판정이 이동 적용 **전** 스냅샷에서 돌기
 * 때문에 `status:"arrived"`는 실제 동석보다 한 wave 늦게 온다 — 이 가드가 없으면 두
 * 아바타가 이미 같은 박스에 서 있는데도 한 wave 동안 짧은 추격선이 남는다.
 * 위치 비교(_agentLoc와 같은 값인 a.loc)가 기준이므로, 같은 박스 안에서 격자 배치상
 * 좌표가 떨어져 있어도 확실히 사라진다.
 *
 * 그 밖에: 둘 중 하나가 미배치(위치 미지정/그래프 밖)이거나 두 아바타가 거의 겹칠 만큼
 * 가까우면 역시 생략한다. lock 상태 자체는 _meetingIntent에 그대로 남아 있어, 다시
 * 떨어지면 선이 되살아난다.
 */
function chaseGeometry(chaser, target) {
  const from = _agents.find(a => a.name === chaser);
  const to   = _agents.find(a => a.name === target);
  if (!from || !to) return null;
  if (from.loc === to.loc) return null;      // 이미 동석 — arrived를 기다리지 않는다
  const p = agentXY(from), q = agentXY(to);
  const dx = q.x - p.x, dy = q.y - p.y;
  const len = Math.hypot(dx, dy);
  const gap = from.r + CHASE_GAP_FROM + to.r + CHASE_GAP_TO;
  if (len <= gap + CHASE_MIN_LEN) return null;
  const ux = dx / len, uy = dy / len;
  const s = from.r + CHASE_GAP_FROM, e = to.r + CHASE_GAP_TO;
  return {
    x1: p.x + ux * s, y1: p.y + uy * s,
    x2: q.x - ux * e, y2: q.y - uy * e,
  };
}

/**
 * _meetingIntent를 <line> 하나씩으로 그린다.
 * 만남 lock이 하나도 없고 화면에 남은 선도 없으면 즉시 반환 — 만남 기능을 안 쓰는
 * 시나리오에서 tick마다 도는 추가 연산이 사실상 0이 된다.
 */
function renderChaseLines(animate) {
  if (!_layers || !_layers.chase) return;
  const keys = Object.keys(_meetingIntent);
  const live = _layers.chase.selectAll('.lm-chase');
  if (!keys.length && live.empty()) return;

  const items = [];
  for (const chaser of keys) {
    const info = _meetingIntent[chaser];
    const geo  = chaseGeometry(chaser, info.target);
    if (geo) items.push({ chaser, ...info, ...geo });
  }

  const sel = live.data(items, d => d.chaser);
  sel.exit().remove();

  const enter = sel.enter().append('line')
    .attr('class', 'lm-chase')
    .attr('marker-end', 'url(#lm-chase-arrow)')
    // 새 선은 (0,0)에서 날아오지 않도록 최종 좌표에 바로 놓는다.
    .attr('x1', d => r2(d.x1)).attr('y1', d => r2(d.y1))
    .attr('x2', d => r2(d.x2)).attr('y2', d => r2(d.y2));
  enter.append('title');

  const all = enter.merge(sel);
  all.select('title').text(d => {
    const chaserLabel = _agents.find(a => a.name === d.chaser)?.label || d.chaser;
    const where = d.targetLocation ? ` (${d.targetLocation})` : '';
    return `${chaserLabel} → ${d.targetName}${where} 만나러 이동 중`;
  });

  const target = animate
    ? all.transition().duration(550).ease(d3.easeCubicInOut)
    : all.interrupt();
  target
    .attr('x1', d => r2(d.x1)).attr('y1', d => r2(d.y1))
    .attr('x2', d => r2(d.x2)).attr('y2', d => r2(d.y2));
}

/**
 * meeting_update SSE 훅 — 만남 lock의 생성/해소를 추격선에 반영한다.
 *   status='start'                → chaser→target 점선 화살표 추가(또는 목표 교체)
 *   status='arrived' | 'cancelled' → 제거
 * 지도가 아직 없으면(위치 그래프 미설정) 상태만 갱신하고 조용히 넘어간다 —
 * 나중에 지도가 만들어지면 renderChaseLines가 알아서 그린다.
 */
export function setMeetingIntentOnMap(d) {
  if (!d || !d.chaser) return;
  if (d.status === 'start') {
    if (!d.target) return;
    _meetingIntent[d.chaser] = {
      target:         d.target,
      targetName:     d.target_name || d.target,
      targetLocation: d.target_location || '',
    };
  } else if (d.status === 'arrived' || d.status === 'cancelled') {
    // 같은 웨이브에 "A 취소 + B 시작"이 순서가 뒤바뀌어 올 경우 방금 세운 lock을
    // 지워버리지 않도록, 목표가 명시돼 있고 다르면 무시한다.
    const cur = _meetingIntent[d.chaser];
    if (!cur || !d.target || cur.target === d.target) delete _meetingIntent[d.chaser];
  } else {
    return;   // 모르는 status — 무시
  }
  renderChaseLines(!!_mapSim && _mapSim.alpha() < 0.05);
}

/** 배치가 끝나면 전체가 보이도록 줌을 맞춘다(사용자가 직접 줌한 뒤에는 건드리지 않음). */
function fitToView() {
  if (_userZoomed || !_gRoot || !_zoom || !_mapData.nodes.length) return;
  const svgEl = document.getElementById('sim-map-svg');
  if (!svgEl) return;
  const W = svgEl.clientWidth, H = svgEl.clientHeight;
  if (!W || !H) return;

  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of _mapData.nodes) {
    if (n.x == null || n.y == null) continue;
    // zone 박스는 halo가 ZONE_PAD만큼 더 나가므로 그만큼 넓게 잡아야 가장자리가 안 잘린다.
    const m = n.zone ? ZONE_PAD : 0;
    x0 = Math.min(x0, n.x - n.w / 2 - m); x1 = Math.max(x1, n.x + n.w / 2 + m);
    y0 = Math.min(y0, n.y - n.h / 2 - m); y1 = Math.max(y1, n.y + n.h / 2 + m);
  }
  if (!Number.isFinite(x0) || !Number.isFinite(y0)) return;

  const bw = Math.max(1, x1 - x0), bh = Math.max(1, y1 - y0);
  const k  = Math.min(1.6, 0.92 * Math.min(W / bw, H / bh));
  const tx = W / 2 - k * (x0 + x1) / 2;
  const ty = H / 2 - k * (y0 + y1) / 2;
  d3.select(svgEl).transition().duration(420)
    .call(_zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k));
}

// ── 외부 API ─────────────────────────────────────────────────────────────────
/**
 * 탭 진입 시 호출. 설정 화면에서 장소 구성이 바뀌었으면 통째로 다시 그리고,
 * 아니면 (숨겨져 있는 동안 잴 수 없었던) 실제 패널 크기만 반영한다.
 */
export function ensureLocationMap() {
  if (graphSignature() !== _signature) { initLocationMap(); return; }
  refreshMapSize();
}

function refreshMapSize() {
  const svgEl = document.getElementById('sim-map-svg');
  if (!svgEl || !_mapSim) return;
  const W = svgEl.clientWidth, H = svgEl.clientHeight;
  if (!W || !H || (W === _W && H === _H)) return;
  _W = W; _H = H;
  _mapSim.force('center', d3.forceCenter(W / 2, H / 2));
  _mapSim.force('x', d3.forceX(W / 2).strength(0.045));
  _mapSim.force('y', d3.forceY(H / 2).strength(0.045));
  _mapSim.alpha(0.3).restart();
}

/**
 * agent_move SSE 훅 — 아바타를 새 장소 박스로 옮긴다.
 * 배치가 이미 안정된 뒤라면(tick이 멈춘 상태) 애니메이션으로, 아직 흔들리는 중이면
 * 즉시 이동한다(tick과 transition이 서로 덮어쓰는 걸 피하기 위해).
 */
export function moveAgentOnMap(agentName, to) {
  if (!_mapSim || !_layers || !agentName) return;
  if (!to || !_mapData.nodeMap[to]) {
    if (to) console.warn('[sim-map] 위치 그래프에 없는 장소로의 이동 무시:', to);
    return;
  }
  if (_agentLoc[agentName] === to) return;
  _agentLoc[agentName] = to;

  renderAgents(_mapSim.alpha() < 0.05);

  const el = _layers.agents.selectAll('.lm-agent').filter(d => d.name === agentName).node();
  if (!el) return;
  el.classList.remove('lm-agent-moved');
  // 리플로우 강제 — 연속 이동 시 CSS 애니메이션을 처음부터 다시 재생시킨다.
  // 탭이 숨겨져 있으면(display:none) 브라우저에 따라 getBBox가 throw하므로 감싼다.
  try { void el.getBBox(); } catch (_) {}
  el.classList.add('lm-agent-moved');
  setTimeout(() => el.classList.remove('lm-agent-moved'), 1400);
}

/** 현재 지도를 SVG 파일로 저장 (관계 그래프의 exportGraph와 동일 패턴). */
export function exportLocationMap() {
  const svgEl = document.getElementById('sim-map-svg');
  if (!svgEl) return;
  const blob = new Blob([new XMLSerializer().serializeToString(svgEl)], { type: 'image/svg+xml' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), { href: url, download: 'sim-location-map.svg' });
  a.click();
  URL.revokeObjectURL(url);
}

/** Test/debug accessor — 살아있는 force simulation과 내부 상태. */
export function getMapSim() {
  return { sim: _mapSim, data: _mapData, agents: _agents, agentLoc: _agentLoc,
           meetingIntent: _meetingIntent };
}
