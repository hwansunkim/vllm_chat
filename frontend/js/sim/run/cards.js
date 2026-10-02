// frontend/js/sim/run/cards.js
// Agent card rendering & live updates on the simulation run view.

import { sim, esc, emotionClass, fmtK, getAgentIcon, infectionBadge, agentStatusBadge } from '../state.js';
import { openAgentContext } from '../context.js';

// 만남 뱃지("→ 목표")용 로컬 상태.
//   _cardLoc   — agent_move로 알게 된 마지막 위치. 지도(map/d3.js)와 별개로 카드가
//                "이미 같은 곳에 있는지"를 판단하는 데만 쓴다.
//   _meetingOf — 해소되지 않은 만남 lock (chaser -> 표시 정보).
// 엔진의 lock 해제가 이동보다 한 wave 늦게 오므로(arrived 지연), 두 값을 함께 봐야
// 이미 만난 뒤에도 뱃지가 한 wave 더 붙어 있는 일이 없다.
let _cardLoc   = {};
let _meetingOf = {};

// 카드 재생성(화면 복귀·설정 변경) 뒤 다시 칠하기 위한 실행 상태 스냅샷.
// 카드 DOM은 언제든 다시 만들어질 수 있으므로 "지금 카드에 보이는 것"의 원천은
// 전부 DOM 밖에 둔다 — 감염은 sim.agentInfection, 상태(수면·이동 등)는 sim.agentStatus,
// 위치·만남은 위의 두 맵, 나머지는 아래 두 맵.
//   _cardSnap — turn_complete로 받은 마지막 메타 필드(누적 병합)·토큰·발언 미리보기
//   _presence — scene_event agent_enter/agent_exit 누적({ entered, exited })
let _cardSnap  = {};
let _presence  = {};
// 이름 → 그 이름으로 상태를 쌓았던 에이전트 **객체**. 상태 맵은 이름으로 키를 잡지만,
// 같은 이름이 다른 에이전트를 가리키게 되는 경우(삭제 후 같은 이름으로 새로 추가, 두
// 에이전트의 이름 맞바꾸기, 설정에서 다른 시나리오 적용 — applyScenario는 새 객체를
// 만든다)를 이름 비교만으로는 못 가려 상태가 엉뚱한 에이전트에게 넘어갔다. 그래서 객체
// 동일성으로 판정한다. 삭제된 이름도 지우지 않고 남겨 둔다(묘비) — 나중에 같은 이름의
// 다른 객체가 생기면 그 사이 쌓인 상태까지 버리기 위해서다.
let _knownAgents = new Map();

/**
 * 실행 상태 초기화 — **새 실행/실행 교체**(새 /start, 이력 불러오기 /load, 재개 /resume)
 * 에서만 부른다. 화면 복귀(views.js)는 카드를 다시 그려도 이 함수를 부르지 않는다 —
 * 예전에는 renderAgentCards()가 매번 이 초기화를 함께 해서, 설정·다른 화면을 다녀오면
 * 감염·수면·감정 등이 사라졌다. 이어서 실행(/continue)은 같은 실행의 연장이라 부르지 않는다.
 */
export function resetRunState() {
  sim.agentEmotions  = {};
  sim.agentInfection = {};
  sim.agentStatus    = {};
  _cardLoc   = {};
  _meetingOf = {};
  _cardSnap  = {};
  _presence  = {};
  _knownAgents = new Map();
}

function dropAgentState(name) {
  delete sim.agentEmotions[name];
  delete sim.agentInfection[name];
  if (sim.agentStatus) delete sim.agentStatus[name];
  delete _cardLoc[name];
  delete _meetingOf[name];
  delete _cardSnap[name];
  delete _presence[name];
}

/**
 * 현재 sim.agents 구성에 맞춰 실행 상태를 정리한다(멱등). 이름이 가리키는 에이전트 객체가
 * 직전과 다르면(삭제·개명·이름 재사용·맞바꾸기·다른 시나리오 적용) 그 이름의 상태를 버린다.
 * 개명은 상태를 새 이름으로 옮기지 않는다 — 수용안대로 "개명된 에이전트는 초기값"이며,
 * /continue는 에이전트 목록을 다시 보내지 않아 서버는 여전히 옛 이름으로 이벤트를 보낸다.
 * 만나러 가던 대상이 버려진 경우 그 만남 뱃지도 함께 버린다.
 * renderAgentCards()가 부르며, 카드를 그리기 전에 위치 스냅샷(getCurrentCardLocations)을
 * 떠야 하는 화면 복귀 경로(views.js)는 그보다 먼저 직접 부른다.
 */
export function syncAgentRoster() {
  const current = new Map(sim.agents.map(a => [a.name, a]));
  const dropped = new Set();
  for (const [name, obj] of _knownAgents) {
    if (current.get(name) !== obj) dropped.add(name);
  }
  for (const name of dropped) dropAgentState(name);
  for (const [chaser, m] of Object.entries(_meetingOf)) {
    if (dropped.has(m.target)) delete _meetingOf[chaser];
  }
  for (const [name, obj] of current) _knownAgents.set(name, obj);
}

/**
 * 카드 DOM을 (다시) 그리고, DOM 밖에 저장된 실행 상태를 다시 칠한다.
 * 실행 상태는 비우지 않는다 — 새 실행이면 호출부가 먼저 resetRunState()를 부른다.
 *
 * @param {Object|null} overrideLocations - agent name -> 실제(복원된) 위치.
 *   /resume·/load처럼 시나리오 설정의 초기 위치가 아니라 저장된 실제 위치로
 *   시작해야 하는 경로가 넘긴다. 없으면 이미 알고 있는 실제 위치(_cardLoc),
 *   그것도 없으면(새 /start 직후 등) sim.agents[i].location을 쓴다.
 */
export function renderAgentCards(overrideLocations = null) {
  syncAgentRoster();
  const container = document.getElementById('sim-agent-cards');
  container.innerHTML = '';
  sim.agents.forEach(agent => {
    const card = document.createElement('div');
    const inactive = agent.initial_active === false;
    card.className = `sim-agent-card${inactive ? ' inactive' : ''}`;
    // Use CSS.escape so non-ASCII / special-char agent names produce valid IDs/selectors.
    card.id = `simc-${CSS.escape(agent.name)}`;
    card.title = '클릭하면 컨텍스트 윈도우 확인';
    card.style.cursor = 'pointer';
    const displayLabel = agent.display_name
      ? `${esc(agent.display_name)}<small style="color:#94a3b8;font-weight:400"> (${esc(agent.name)})</small>`
      : esc(agent.name);
    const metaHtml = sim.extra_fields.map(f => {
      const cls = f.name === 'emotion'
        ? `sim-feed-badge ${emotionClass(f.default)}`
        : 'sim-feed-badge emotion-neutral';
      return `<span class="${cls}" id="simc-meta-${esc(f.name)}-${esc(agent.name)}">${esc(f.default)}</span>`;
    }).join('');
    // 초기 위치도 만남 뱃지의 "이미 같은 곳" 판정에 쓰이므로 함께 기록해둔다
    // (한 번도 안 움직인 두 사람이 처음부터 같은 방에 있는 경우).
    const initialLoc = (overrideLocations && overrideLocations[agent.name])
      || _cardLoc[agent.name] || agent.location;
    if (initialLoc) _cardLoc[agent.name] = initialLoc;
    const locHtml = initialLoc
      ? `<span class="sim-card-location" id="simc-loc-${esc(agent.name)}">📍 ${esc(initialLoc)}</span>`
      : `<span class="sim-card-location sim-hidden" id="simc-loc-${esc(agent.name)}"></span>`;

    card.innerHTML = `
      <div class="sim-card-header">
        <span class="sim-card-icon" id="simc-icon-${esc(agent.name)}">${esc(getAgentIcon(agent, 'neutral'))}</span>
        <span class="sim-card-name">${displayLabel}</span>
        <span class="sim-card-infection sim-hidden" id="simc-inf-${esc(agent.name)}"></span>
        <span class="sim-card-status sim-hidden" id="simc-status-${esc(agent.name)}"></span>
        <span class="sim-card-meeting sim-hidden" id="simc-meet-${esc(agent.name)}"></span>
        ${locHtml}
      </div>
      <div class="sim-card-meta">${metaHtml}</div>
      <div class="sim-card-token-row">
        <div class="sim-card-token-bar-wrap">
          <div class="sim-card-token-bar-fill" id="simc-tok-${esc(agent.name)}" style="width:0%"></div>
        </div>
        <span class="sim-card-token-label" id="simc-tokl-${esc(agent.name)}">— / ${fmtK(sim.token_limit)}</span>
      </div>
      <div class="sim-card-preview" id="simc-pre-${esc(agent.name)}">대기 중...</div>
    `;
    card.addEventListener('click', () => openAgentContext(agent.name));
    container.appendChild(card);
  });
  // 새 카드는 전부 기본값(배지 숨김·"대기 중...")으로 생겼다 — 저장된 실행 상태를 다시 칠한다.
  // SSE 핸들러가 쓰는 것과 같은 그리기 함수를 그대로 재사용한다.
  sim.agents.forEach(agent => restoreCardState(agent.name));
  refreshMeetingBadges();
}

/** 한 에이전트 카드에 저장된 상태(메타·토큰·발언·감염·상태·등퇴장)를 다시 칠한다. */
function restoreCardState(name) {
  const snap = _cardSnap[name];
  if (snap) paintAgentCard(name, snap.meta, snap.promptTokens, snap.tokenLimit, snap.preview);
  if (sim.agentInfection[name]) applyInfectionBadge(name);
  if (sim.agentStatus?.[name]) applyStatusBadge(name);
  if (_presence[name]) applyPresence(name);
}

export function updateAgentCard(speaker, meta, promptTokens, tokenLimit, preview) {
  // DOM 갱신과 같은 규칙으로 스냅샷을 쌓는다(메타는 필드별 덮어쓰기, 토큰·미리보기는 값이
  // 있을 때만) — 카드를 다시 만들 때 paintAgentCard에 그대로 넣으면 같은 화면이 된다.
  const snap = _cardSnap[speaker] || (_cardSnap[speaker] = { meta: {} });
  Object.assign(snap.meta, meta || {});
  if (promptTokens && tokenLimit) { snap.promptTokens = promptTokens; snap.tokenLimit = tokenLimit; }
  if (preview) snap.preview = preview;
  paintAgentCard(speaker, meta, promptTokens, tokenLimit, preview);
}

function paintAgentCard(speaker, meta, promptTokens, tokenLimit, preview) {
  Object.entries(meta || {}).forEach(([field, value]) => {
    const el = document.getElementById(`simc-meta-${field}-${speaker}`);
    if (!el) return;
    el.textContent = value;
    if (field === 'emotion') {
      el.className = `sim-feed-badge ${emotionClass(String(value))}`;
      const iconEl = document.getElementById(`simc-icon-${speaker}`);
      if (iconEl) {
        const agent = sim.agents.find(a => a.name === speaker);
        if (agent) iconEl.textContent = getAgentIcon(agent, String(value));
      }
    }
  });

  if (promptTokens && tokenLimit) {
    const pct = Math.min(100, (promptTokens / tokenLimit) * 100);
    const barEl = document.getElementById(`simc-tok-${speaker}`);
    const lblEl = document.getElementById(`simc-tokl-${speaker}`);
    if (barEl) {
      barEl.style.width = `${pct}%`;
      barEl.className   = `sim-card-token-bar-fill${pct >= 90 ? ' danger' : pct >= 70 ? ' warn' : ''}`;
    }
    if (lblEl) lblEl.textContent = `${fmtK(promptTokens)} / ${fmtK(tokenLimit)}`;
  }

  const preEl = document.getElementById(`simc-pre-${speaker}`);
  if (preEl && preview) preEl.textContent = preview.slice(0, 42);
}

/** Update the location badge on an agent card after a move event. */
export function updateAgentLocation(agentName, location) {
  if (location) _cardLoc[agentName] = location;
  const el = document.getElementById(`simc-loc-${CSS.escape(agentName)}`);
  if (el && location) {
    el.textContent = `📍 ${location}`;
    el.classList.remove('sim-hidden');
  }
  // 이동으로 "만나러 가던 사람과 같은 곳"이 됐을 수 있다 — 관련 뱃지를 다시 판정한다.
  // (엔진의 arrived는 한 wave 늦게 오므로 여기서 먼저 걷어낸다.)
  if (location) refreshMeetingBadges();
}

/**
 * infection_update SSE 훅 — 카드에 감염 상태 뱃지를 붙이고 상태 맵을 갱신한다.
 * 상태 맵(sim.agentInfection)은 관계 그래프·위치 지도의 노드 강조도 함께 참조한다.
 */
export function updateAgentInfection(d) {
  if (!d || !d.agent) return;
  sim.agentInfection[d.agent] = {
    status:       d.status,
    cause:        d.cause,
    wave:         d.wave,
    disease_name: d.disease_name || '',
  };

  applyInfectionBadge(d.agent);
}

/** sim.agentInfection[name] 그대로 카드 감염 뱃지를 칠한다(카드가 없으면 무시). */
function applyInfectionBadge(name) {
  // 카드 내부 요소의 id는 esc()(HTML 이스케이프)로 쓰였으므로 실제 DOM id는 원본 이름
  // 그대로다 — getElementById에 CSS.escape를 끼우면 오히려 어긋난다(updateAgentCard와 동일).
  const el = document.getElementById(`simc-inf-${name}`);
  if (!el) return;
  const rec   = sim.agentInfection[name];
  const badge = rec ? infectionBadge(rec.status, rec.cause) : null;
  if (!badge) {                       // 한 번도 걸리지 않은 S — 표시할 것 없음
    el.classList.add('sim-hidden');
    el.textContent = '';
    return;
  }
  el.textContent = `${badge.icon} ${badge.label}`;
  el.className   = `sim-card-infection inf-${badge.cls}`;
  el.title       = rec.disease_name ? `${rec.disease_name} · W${rec.wave}` : `W${rec.wave}`;
  getCardEl(name)?.classList.toggle('infected', badge.cls === 'infected');
}

/**
 * agent_status_change SSE 훅 — 카드에 상태(수면·개인 용무·이동) 뱃지를 붙이거나
 * (해제 시) 지운다. infection과 달리 "과거 이력"은 남기지 않고 지금 활성인 상태만
 * sim.agentStatus에 둔다(해제되면 항목 삭제). 카드가 없을 때(다른 화면에 가 있어
 * 카드가 다시 그려지기 전 등)도 맵은 갱신해야 복귀 시 최신 상태로 다시 칠할 수 있다.
 */
export function updateAgentStatus(d) {
  if (!d || !d.agent) return;
  if (!sim.agentStatus) sim.agentStatus = {};
  if (agentStatusBadge(d)) sim.agentStatus[d.agent] = d;
  else delete sim.agentStatus[d.agent];
  applyStatusBadge(d.agent);
}

/** sim.agentStatus[name] 그대로 카드 상태 뱃지를 칠한다(카드가 없으면 무시). */
function applyStatusBadge(name) {
  const el = document.getElementById(`simc-status-${name}`);
  if (!el) return;
  const d = sim.agentStatus?.[name];
  const badge = d ? agentStatusBadge(d) : null;
  if (!badge) {
    el.classList.add('sim-hidden');
    el.textContent = '';
    el.title = '';
    return;
  }
  el.textContent = `${badge.icon} ${badge.label}`;
  el.className   = `sim-card-status st-${badge.cls}`;
  el.title = d.until_time_str ? `${d.until_time_str}까지 (약 ${d.minutes}분)` : '';
}

/**
 * scene_event의 agent_enter/agent_exit를 카드 등퇴장 표시에 반영한다(누적 기록 후 칠함).
 * enter는 'inactive'를 떼고, exit는 'exited'를 붙인다 — 예전 sse.js 인라인 처리와 동일.
 */
export function updateAgentPresence(agent, eventType) {
  if (!agent) return;
  const p = _presence[agent] || (_presence[agent] = { entered: false, exited: false });
  if (eventType === 'agent_enter') p.entered = true;
  else if (eventType === 'agent_exit') p.exited = true;
  else return;
  applyPresence(agent);
}

function applyPresence(name) {
  const card = getCardEl(name);
  const p = _presence[name];
  if (!card || !p) return;
  if (p.entered) card.classList.remove('inactive');
  if (p.exited) card.classList.add('exited');
}

/**
 * meeting_update SSE 훅 — chaser 카드에 "→ 목표" 소형 뱃지를 붙이거나 지운다.
 * 만나러 가는 동안만 보이며 도착/취소 시 사라진다. 카드가 없으면(구성 변경 직후 등)
 * 조용히 넘어간다 — 추격선/피드는 이 함수와 무관하게 각자 갱신된다.
 *
 * id는 renderAgentCards에서 esc()(HTML 이스케이프)로 쓰였으므로 실제 DOM id는 원본
 * 이름 그대로다 — getElementById에 CSS.escape를 끼우면 어긋난다(updateAgentInfection과 동일).
 */
export function updateAgentMeetingBadge(d) {
  if (!d || !d.chaser) return;
  const label = d.target_name || d.target || '';
  if (d.status === 'start' && d.target && label) {
    // target_location은 그 wave의 이동을 적용하기 **전** 위치다. 뒤이어 오는 agent_move가
    // 실제 위치를 알려주므로 여기서는 툴팁 참고용으로만 쓴다.
    _meetingOf[d.chaser] = { target: d.target, label, location: d.target_location || '' };
  } else if (d.status === 'arrived' || d.status === 'cancelled') {
    // 같은 wave에 "A 취소 + B 시작"이 뒤바뀌어 와도 방금 세운 lock을 지우지 않는다.
    const cur = _meetingOf[d.chaser];
    if (cur && d.target && cur.target !== d.target) return;
    delete _meetingOf[d.chaser];
  } else {
    return;                       // 모르는 status — 무시
  }
  applyMeetingBadge(d.chaser);
}

/** 한 chaser의 뱃지를 현재 lock/위치 상태대로 다시 그린다. */
function applyMeetingBadge(chaser) {
  const el = document.getElementById(`simc-meet-${chaser}`);
  if (!el) return;
  const m = _meetingOf[chaser];
  // 이미 같은 곳에 있으면 숨긴다 — arrived가 한 wave 늦게 오는 걸 기다리지 않는다.
  const together = m && _cardLoc[chaser] && _cardLoc[chaser] === _cardLoc[m.target];
  if (!m || together) {
    el.textContent = '';
    el.removeAttribute('title');
    el.classList.add('sim-hidden');
    return;
  }
  el.textContent = `→ ${m.label}`;
  el.title = m.location
    ? `${m.label}을(를) 만나러 이동 중 (${m.location})`
    : `${m.label}을(를) 만나러 이동 중`;
  el.classList.remove('sim-hidden');
}

/** 살아있는 lock 전부를 다시 판정 (이동으로 위치 관계가 바뀐 뒤 호출). */
function refreshMeetingBadges() {
  for (const chaser of Object.keys(_meetingOf)) applyMeetingBadge(chaser);
}

/** Lookup the live card element by agent name, handling special characters. */
export function getCardEl(name) {
  return document.getElementById(`simc-${CSS.escape(name)}`);
}

/**
 * 지금까지 파악된 각 에이전트의 실제 위치 스냅샷(agent name -> location).
 * `renderAgentCards()`를 다시 부르기 **직전**에 떠서 그 호출의
 * `overrideLocations`로 되돌려주는 용도 — 설정 화면을 열었다 닫을 때처럼
 * 카드를 다시 그려야 하지만 실행 중인(또는 중지된) 시뮬레이션의 실제 위치는
 * 잃으면 안 되는 경우에 쓴다. 새 이름의 에이전트는 여기 없으므로 호출부의
 * 기본 폴백(`agent.location`)으로 자연히 넘어간다.
 */
export function getCurrentCardLocations() {
  return { ..._cardLoc };
}
