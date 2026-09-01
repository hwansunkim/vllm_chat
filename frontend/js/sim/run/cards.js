// frontend/js/sim/run/cards.js
// Agent card rendering & live updates on the simulation run view.

import { sim, esc, emotionClass, fmtK, getAgentIcon, infectionBadge } from '../state.js';
import { openAgentContext } from '../context.js';

// 만남 뱃지("→ 목표")용 로컬 상태.
//   _cardLoc   — agent_move로 알게 된 마지막 위치. 지도(map/d3.js)와 별개로 카드가
//                "이미 같은 곳에 있는지"를 판단하는 데만 쓴다.
//   _meetingOf — 해소되지 않은 만남 lock (chaser -> 표시 정보).
// 엔진의 lock 해제가 이동보다 한 wave 늦게 오므로(arrived 지연), 두 값을 함께 봐야
// 이미 만난 뒤에도 뱃지가 한 wave 더 붙어 있는 일이 없다.
let _cardLoc   = {};
let _meetingOf = {};

export function renderAgentCards() {
  sim.agentEmotions  = {};
  sim.agentInfection = {};
  _cardLoc   = {};
  _meetingOf = {};
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
    if (agent.location) _cardLoc[agent.name] = agent.location;
    const locHtml = agent.location
      ? `<span class="sim-card-location" id="simc-loc-${esc(agent.name)}">📍 ${esc(agent.location)}</span>`
      : `<span class="sim-card-location sim-hidden" id="simc-loc-${esc(agent.name)}"></span>`;

    card.innerHTML = `
      <div class="sim-card-header">
        <span class="sim-card-icon" id="simc-icon-${esc(agent.name)}">${esc(getAgentIcon(agent, 'neutral'))}</span>
        <span class="sim-card-name">${displayLabel}</span>
        <span class="sim-card-infection sim-hidden" id="simc-inf-${esc(agent.name)}"></span>
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
}

export function updateAgentCard(speaker, meta, promptTokens, tokenLimit, preview) {
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

  // 카드 내부 요소의 id는 esc()(HTML 이스케이프)로 쓰였으므로 실제 DOM id는 원본 이름
  // 그대로다 — getElementById에 CSS.escape를 끼우면 오히려 어긋난다(updateAgentCard와 동일).
  const el = document.getElementById(`simc-inf-${d.agent}`);
  if (!el) return;
  const badge = infectionBadge(d.status, d.cause);
  if (!badge) {                       // 한 번도 걸리지 않은 S — 표시할 것 없음
    el.classList.add('sim-hidden');
    el.textContent = '';
    return;
  }
  el.textContent = `${badge.icon} ${badge.label}`;
  el.className   = `sim-card-infection inf-${badge.cls}`;
  el.title       = d.disease_name ? `${d.disease_name} · W${d.wave}` : `W${d.wave}`;
  getCardEl(d.agent)?.classList.toggle('infected', badge.cls === 'infected');
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
