// frontend/js/sim/run/feed.js
// Live feed: typing indicator, agent messages, scene events, wave indicator.

import { sim, esc, emotionClass, agentLabel, getAgentIcon, simTimeLabel, infectionBadge,
         meetingNarration } from '../state.js';

// 증상 서사 카드 접두사 — 서버(_build_symptom_context)가 항상 이 머리말로 시작한다.
// 같은 턴에 위치 안내 카드와 증상 카드가 각각 올 수 있어 이걸로 구분해 다르게 꾸민다.
const SYMPTOM_PREFIX = '[몸 상태]';

// ── 디렉터 카드의 wave 정렬 ──────────────────────────────────────────────────
// system_intervention / world_event 의 `wave` 는 "그 개입이 실제로 소비되는 wave"다.
// 엔진이 디렉터를 wave 루프 **상단**(wave_start emit 앞)에서 돌리므로, 이 이벤트는
// 해당 wave 의 wave_start 보다 **먼저** 도착한다. 도착 순서대로 붙이면 카드가
// 직전 wave 구분선 아래에 놓이면서 라벨(W N)과 위치가 어긋난다.
// 그래서 아직 시작되지 않은 wave 의 카드는 잠시 들고 있다가, 그 wave 의 구분선을
// 그린 직후에 흘려보낸다. 결과 순서: 구분선(W N) → 디렉터 카드(W N) → W N 의 턴들.
let _currentWave  = null;   // updateWaveIndicator() 가 마지막으로 처리한 wave
let _pendingCards = [];     // [{ wave, el }] — 아직 시작 안 된 wave 의 카드

/** 새 실행(또는 이력 복원)로 피드를 비울 때 wave 버퍼도 함께 비운다. */
export function resetWaveCardBuffer() {
  _currentWave  = null;
  _pendingCards = [];
}

function _appendWaveCard(wave, el) {
  // wave 를 모르는 구버전 페이로드는 판단할 근거가 없으니 그대로 붙인다.
  if (wave != null && _currentWave != null && wave > _currentWave) {
    _pendingCards.push({ wave, el });
    return;
  }
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

/** 이 wave 까지의 보류 카드를 순서대로 흘려보낸다. simulation_end 에서는 전부. */
export function flushPendingWaveCards(uptoWave) {
  if (!_pendingCards.length) return;
  const feed = document.getElementById('sim-feed');
  const stay = [];
  let last = null;
  for (const item of _pendingCards) {
    if (uptoWave == null || item.wave <= uptoWave) {
      feed.appendChild(item.el);
      last = item.el;
    } else {
      stay.push(item);
    }
  }
  _pendingCards = stay;
  last?.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function renderHistoricalFeed(entries) {
  const feed = document.getElementById('sim-feed');
  feed.innerHTML = '';
  resetWaveCardBuffer();

  if (!entries || !entries.length) {
    feed.innerHTML = '<div id="sim-feed-empty">저장된 대화 기록이 없습니다.</div>';
    return;
  }

  let curWave = null;
  entries.forEach(entry => {
    if (entry.wave != null && entry.wave !== curWave) {
      curWave = entry.wave;
      // 서버가 저장해둔 time_str(정확값)을 우선 사용하고, 없는 구버전 로그는 fixed 공식으로 폴백.
      const timeLabel = entry.time_str ?? simTimeLabel(curWave);
      if (timeLabel) addWaveDivider(curWave, timeLabel);
    }

    const agent = sim.agents.find(a => a.name === entry.speaker) || { icon: '🤖', name: entry.speaker };
    const targets = (entry.targets || []).filter(t => t !== 'self' && t !== 'system');
    const targetLabels = targets.map(t => t === 'all' ? '전체' : agentLabel(t));
    const targetStr = targets.length ? `→ ${targetLabels.join(', ')}` : '(독백)';

    const meta = entry.meta || {};
    const entryEmotion = meta.emotion || 'neutral';
    const metaBadges = Object.entries(meta)
      .filter(([k]) => k !== 'action_note')
      .map(([k, v]) =>
        `<span class="sim-feed-badge ${k === 'emotion' ? emotionClass(String(v)) : 'emotion-neutral'}">${esc(String(v))}</span>`)
      .join('');
    const actionNote = entry.action_note || '';
    const waveBadge  = entry.wave != null ? `<span class="sim-feed-wave-mini">W${entry.wave}</span>` : '';

    const div = document.createElement('div');
    div.className = 'sim-feed-msg sim-feed-msg-history';
    div.innerHTML = `
      <div class="sim-feed-header">
        <span class="sim-feed-speaker">${esc(getAgentIcon(agent, entryEmotion))} ${esc(agent.display_name || agent.name)}</span>
        ${waveBadge}
        <span class="sim-feed-target">${esc(targetStr)}</span>
      </div>
      <div class="sim-feed-bubble">${esc(entry.content)}</div>
      ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
      ${metaBadges ? `<div class="sim-feed-meta">${metaBadges}</div>` : ''}
    `;
    feed.appendChild(div);
  });

  feed.lastElementChild?.scrollIntoView({ block: 'end' });
}

export function removeFeedEmpty() {
  const el = document.getElementById('sim-feed-empty');
  if (el) el.remove();
}

export function addTypingIndicator(speaker) {
  if (document.getElementById(`sim-typing-${speaker}`)) return;
  removeFeedEmpty();
  const agent = sim.agents.find(a => a.name === speaker) || { icon: '🤖', name: speaker };
  const el = document.createElement('div');
  el.id = `sim-typing-${speaker}`;
  el.className = 'sim-typing-row';
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(getAgentIcon(agent, sim.agentEmotions[speaker]))} ${esc(agent.display_name || agent.name)}</span>
    </div>
    <div class="sim-typing-bubble">
      <div class="sim-typing-dots"><span></span><span></span><span></span></div>
      <span class="sim-typing-label">생성 중...</span>
    </div>
  `;
  const feed = document.getElementById('sim-feed');
  feed.appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function removeTypingIndicator(speaker) {
  if (speaker) {
    document.getElementById(`sim-typing-${speaker}`)?.remove();
  } else {
    document.querySelectorAll('[id^="sim-typing-"]').forEach(el => el.remove());
  }
}

export function addFeedMessage(data) {
  removeTypingIndicator(data.speaker);
  const agent = sim.agents.find(a => a.name === data.speaker) || { icon: '🤖', name: data.speaker };
  const targets = data.targets.filter(t => t !== 'self' && t !== 'system');
  const targetLabels = targets.map(t => {
    if (t === 'all') return '전체';
    return agentLabel(t);
  });
  const targetStr = data.is_exterior
    ? '(외부 공간 — 독백)'
    : targets.length
      ? `→ ${targetLabels.join(', ')}`
      : '(독백)';
  const el = document.createElement('div');
  el.className = data.is_exterior ? 'sim-feed-msg sim-feed-msg-exterior' : 'sim-feed-msg';
  const meta = data.meta || {};
  const msgEmotion = meta.emotion || 'neutral';
  const metaBadges = Object.entries(meta)
    .filter(([k]) => k !== 'action_note')
    .map(([k, v]) =>
      `<span class="sim-feed-badge ${k === 'emotion' ? emotionClass(String(v)) : 'emotion-neutral'}">${esc(String(v))}</span>`
    ).join('');

  const actionNote = data.action_note || '';
  el.innerHTML = `
    <div class="sim-feed-header">
      <span class="sim-feed-speaker">${esc(getAgentIcon(agent, msgEmotion))} ${esc(agent.display_name || agent.name)}</span>
      <span class="sim-feed-target">${esc(targetStr)}</span>
    </div>
    <div class="sim-feed-bubble">${esc(data.content)}</div>
    ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
    ${metaBadges ? `<div class="sim-feed-meta">${metaBadges}</div>` : ''}
    ${data.reasoning_preview
      ? `<div class="sim-feed-thinking">🧠 ${esc(data.reasoning_preview)}...</div>`
      : ''}
  `;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addInterventionCard(d) {
  removeFeedEmpty();
  const targetLabel = d.target_alias || d.target;
  const el          = document.createElement('div');
  el.className      = 'sim-intervention-card';
  el.innerHTML = `
    <div class="sim-intervention-header">
      <span class="sim-intervention-icon">${esc(d.icon || '🎬')}</span>
      <span class="sim-intervention-name">${esc(d.display_name || '내레이터')}</span>
      <span class="sim-intervention-arrow">→</span>
      <span class="sim-intervention-target">${esc(targetLabel)}</span>
      <span class="sim-intervention-wave">W${d.wave}</span>
    </div>
    <div class="sim-intervention-msg">${esc(d.message || '')}</div>
    ${d.reason ? `<div class="sim-intervention-reason">${esc(d.reason)}</div>` : ''}`;
  _appendWaveCard(d.wave, el);
}

export function addSummaryCard(d) {
  removeFeedEmpty();
  const range      = d.wave_start === d.wave_end ? `Wave ${d.wave_start}` : `Wave ${d.wave_start}–${d.wave_end}`;
  const keyEvents  = (d.key_events || []).map(e => `<li>${esc(e)}</li>`).join('');
  const el         = document.createElement('div');
  el.className     = 'sim-summary-card';
  el.innerHTML = `
    <div class="sim-summary-header">
      <span class="sim-summary-icon">📊</span>
      <span class="sim-summary-range">${esc(range)} 요약</span>
      ${d.mood ? `<span class="sim-summary-mood">${esc(d.mood)}</span>` : ''}
    </div>
    <div class="sim-summary-body">
      <p class="sim-summary-text">${esc(d.summary || '')}</p>
      ${keyEvents ? `<ul class="sim-summary-events">${keyEvents}</ul>` : ''}
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addWorldEventCard(d) {
  removeFeedEmpty();
  const targetStr = (d.target_aliases || d.targets || []).join(', ') || '전체';
  const el = document.createElement('div');
  el.className = 'sim-world-event-card';
  el.innerHTML = `
    <div class="sim-world-event-header">
      <span class="sim-world-event-icon">🌍</span>
      <span class="sim-world-event-label">세계 사건</span>
      <span class="sim-world-event-targets">→ ${esc(targetStr)}</span>
      <span class="sim-world-event-wave">W${d.wave}</span>
    </div>
    <div class="sim-world-event-content">${esc(d.content || '')}</div>
    ${d.reason ? `<div class="sim-world-event-reason">${esc(d.reason)}</div>` : ''}`;
  _appendWaveCard(d.wave, el);
}

export function addSceneEventToFeed(d) {
  removeFeedEmpty();
  const icons  = { system_message: '📢', agent_enter: '🎭', agent_exit: '🚪', infect_agent: '🦠' };
  const labels = { system_message: '시스템', agent_enter: '등장', agent_exit: '퇴장', infect_agent: '감염 시드' };
  const icon  = icons[d.event_type]  || '📌';
  const label = labels[d.event_type] || d.event_type;
  const agentHint = d.agent ? ` (${agentLabel(d.agent)})` : '';
  const el = document.createElement('div');
  // observer_only = 어떤 에이전트 메모리에도 들어가지 않은 관전자 전용 메시지.
  // 등장인물이 실제로 들은 말과 헷갈리지 않도록 회색 이탤릭으로 구분한다.
  el.className = `sim-scene-event${d.observer_only ? ' sim-scene-event-observer' : ''}`;
  el.innerHTML = `
    <div class="sim-scene-event-icon">${icon}</div>
    <div class="sim-scene-event-body">
      <div class="sim-scene-event-type">${label}${esc(agentHint)}${
        d.observer_only ? '<span class="sim-observer-tag">관전자 전용</span>' : ''}</div>
      <div class="sim-scene-event-msg">${esc(d.message || '')}</div>
    </div>
  `;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

/** infection_update SSE — 감염/전파/회복 한 줄. (엔진 판정 결과이며 LLM 발화가 아니다.) */
export function addInfectionCard(d) {
  removeFeedEmpty();
  const badge = infectionBadge(d.status, d.cause);
  if (!badge) return;                       // 표시할 상태 변화가 아님
  const causeLabel = {
    event:        '시드 (환자 0번)',
    transmission: '접촉 전파',
    recovery:     '회복',
  }[d.cause] || d.cause || '';
  const disease = d.disease_name ? `${d.disease_name} · ` : '';
  const el = document.createElement('div');
  el.className = `sim-infection-card inf-${badge.cls}`;
  el.innerHTML = `
    <div class="sim-infection-header">
      <span class="sim-infection-icon">${badge.icon}</span>
      <span class="sim-infection-name">${esc(d.display_name || agentLabel(d.agent))}</span>
      <span class="sim-infection-label">${esc(badge.label)}</span>
      <span class="sim-infection-cause">${esc(disease + causeLabel)}</span>
      <span class="sim-infection-wave">W${d.wave}</span>
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addMovementCard(d) {
  removeFeedEmpty();
  const fromStr = d.from ? esc(d.from) : '(미설정)';
  const toStr   = esc(d.to || '');
  const el      = document.createElement('div');
  el.className  = 'sim-movement-card';
  el.innerHTML = `
    <div class="sim-movement-header">
      <span class="sim-movement-icon">🚶</span>
      <span class="sim-movement-name">${esc(d.display_name || d.agent)}</span>
      <span class="sim-movement-arrow">이동</span>
      <span class="sim-movement-route">${fromStr} → ${toStr}</span>
      <span class="sim-movement-wave">W${d.wave}</span>
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

/**
 * meeting_update SSE — 만남 lock의 생성/해소 한 줄.
 * 문구는 state.js의 meetingNarration 한 곳에서만 만든다(마크다운 내보내기와 공유).
 * 모르는 status면 null이 와서 카드를 만들지 않는다 — 만남을 안 쓰는 시나리오는 이벤트
 * 자체가 0건이므로 피드에 아무 변화가 없다.
 */
export function addMeetingCard(d) {
  const info = meetingNarration(d);
  if (!info) return;
  removeFeedEmpty();
  const el     = document.createElement('div');
  el.className = `sim-meeting-card meet-${info.cls}`;
  el.innerHTML = `
    <div class="sim-meeting-header">
      <span class="sim-meeting-icon">${info.icon}</span>
      <span class="sim-meeting-text">${esc(info.text)}</span>
      ${d.wave != null ? `<span class="sim-meeting-wave">W${esc(String(d.wave))}</span>` : ''}
    </div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addAppearanceCard(d) {
  removeFeedEmpty();
  const el     = document.createElement('div');
  el.className = 'sim-appearance-card';
  el.innerHTML = `
    <div class="sim-appearance-header">
      <span class="sim-appearance-icon">🪞</span>
      <span class="sim-appearance-name">${esc(d.display_name || d.agent)}</span>
      <span class="sim-appearance-label">외모 변경</span>
      <span class="sim-appearance-wave">W${d.wave}</span>
    </div>
    <div class="sim-appearance-desc">${esc(d.description || '')}</div>`;
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function addSituationCard(d) {
  removeFeedEmpty();
  const agent  = sim.agents.find(a => a.name === d.agent) || { icon: '📍', name: d.agent };
  // 감염 중인 에이전트는 한 턴에 turn_situation이 2번 온다(위치 안내 + 증상 서사).
  // 증상 카드는 텍스트가 '[몸 상태]'로 시작하므로 그걸로 구분해 다른 색/아이콘을 준다.
  const isSymptom = String(d.text || '').startsWith(SYMPTOM_PREFIX);
  const el     = document.createElement('div');
  el.className = `sim-situation-card${isSymptom ? ' sim-situation-symptom' : ''}`;
  el.innerHTML = `
    <div class="sim-situation-header">
      <span class="sim-situation-icon">${isSymptom ? '🤒' : '📍'}</span>
      <span class="sim-situation-name">${esc(agent.display_name || agent.name)}</span>
      <span class="sim-situation-label">${isSymptom ? '몸 상태' : '상황 컨텍스트'}</span>
      <span class="sim-situation-wave">W${d.wave}</span>
      <span class="sim-situation-toggle">▶</span>
    </div>
    <pre class="sim-situation-body sim-hidden">${esc(d.text)}</pre>
  `;
  const header = el.querySelector('.sim-situation-header');
  const body   = el.querySelector('.sim-situation-body');
  const toggle = el.querySelector('.sim-situation-toggle');
  header.addEventListener('click', () => {
    const hidden = body.classList.toggle('sim-hidden');
    toggle.textContent = hidden ? '▶' : '▼';
  });
  document.getElementById('sim-feed').appendChild(el);
  el.scrollIntoView({ behavior: 'smooth', block: 'end' });
}

export function updateWaveIndicator(waveNum, agents) {
  const labels = agents.map(k => agentLabel(k));
  document.getElementById('sim-turn-text').textContent =
    `Wave ${waveNum}  |  ${labels.join(', ')}`;
  document.getElementById('sim-progress-fill').style.width = '30%';

  // wave_start 시점에는 서버가 아직 이번 wave의 실제 time_str을 계산하지 않았다
  // (variable 모드는 wave의 대화 내용을 봐야 분류 가능). fixed 모드 폴백 공식으로
  // 잠정 표시해두고, 이후 turn_complete 이벤트의 time_str이 도착하면 applyWaveTimeStr()가
  // 정확한 값으로 덮어쓴다.
  const fallbackLabel = simTimeLabel(waveNum);
  const timeEl = document.getElementById('sim-turn-time');
  if (timeEl) {
    timeEl.dataset.wave = String(waveNum);
    if (fallbackLabel) {
      timeEl.textContent = `🕐 ${fallbackLabel}`;
      timeEl.classList.remove('sim-hidden');
    } else if (sim.time_mode === 'variable') {
      timeEl.textContent = '🕐 …';
      timeEl.classList.remove('sim-hidden');
    } else {
      timeEl.classList.add('sim-hidden');
    }
  }

  if (fallbackLabel || sim.time_mode === 'variable') {
    addWaveDivider(waveNum, fallbackLabel);
  }

  // 구분선을 그린 **뒤에** 이 wave 의 디렉터 카드를 흘려보낸다 — 카드의 W 라벨과
  // 실제로 놓이는 자리가 일치하도록. (엔진이 wave_start 앞에서 디렉터를 돌린다.)
  _currentWave = waveNum;
  flushPendingWaveCards(waveNum);
}

function addWaveDivider(waveNum, timeLabel) {
  removeFeedEmpty();
  const feed = document.getElementById('sim-feed');
  const div = document.createElement('div');
  div.className = 'sim-wave-divider';
  div.dataset.wave = waveNum;
  const label = timeLabel || '…';
  div.innerHTML = `<span class="sim-wave-divider-line"></span><span class="sim-wave-divider-label">🕐 ${esc(label)}</span><span class="sim-wave-divider-line"></span>`;
  feed.appendChild(div);
}

/** 서버가 보내온 실제 time_str로 해당 wave의 구분선/시간 뱃지를 갱신한다. */
export function applyWaveTimeStr(waveNum, timeStr) {
  if (!timeStr) return;
  const label = `🕐 ${timeStr}`;
  document.querySelectorAll(`.sim-wave-divider[data-wave="${waveNum}"] .sim-wave-divider-label`)
    .forEach(el => { el.textContent = label; });
  const timeEl = document.getElementById('sim-turn-time');
  if (timeEl && timeEl.dataset.wave === String(waveNum)) {
    timeEl.textContent = label;
    timeEl.classList.remove('sim-hidden');
  }
}
