// frontend/js/sim/runs/interview.js
// 사후 인터뷰(post-simulation interview) 패널.
//
// 끝난 run 하나를 골라 특정 에이전트에게 질문을 던지는 UI. 백엔드에서 인터뷰는
// `interview_log` 에만 저장되고 `simulation_log` / 워킹 메모리에는 절대 들어가지
// 않는다 — UI 에서도 리플레이 피드와 시각적으로 분리해 그 사실을 드러낸다.
//
// API 계약 (backend/api/simulation/runtime.py):
//   POST /api/simulation/runs/{run_id}/agents/{name}/interview
//        body { question, mode: "memory_only"|"full_log", max_tokens? }
//        → { id, run_id, agent_key, mode, question, answer, created_at, meta }
//   GET  /api/simulation/runs/{run_id}/agents/{name}/interview → InterviewRecord[]
//        (id ASC, 없으면 [])

import { esc, getAgentIcon } from '../state.js';
import { fmtTime } from '../utils/time.js';

// ── 상수 ──────────────────────────────────────────────────────────────────────

// 백엔드 runtime._INTERVIEWABLE_STATUS 와 동일하게 유지할 것.
// 'running' 은 agent_snapshots 가 아직 기록되지 않아(finalize_run 시점 저장) 제외된다.
const INTERVIEWABLE_STATUS = ['done', 'stopped', 'error'];

const MODES = {
  memory_only: {
    label: '자신의 기억만',
    icon:  '🧠',
    hint:  '에이전트가 직접 겪고 기억하는 것만으로 답합니다. 못 본 장면은 모른다고 말합니다.',
  },
  full_log: {
    label: '전체 로그 참고',
    icon:  '📜',
    hint:  '에이전트가 모든 참여자의 기록을 다시 읽어본 상태로 메타적으로 회고합니다.',
  },
};
const DEFAULT_MODE = 'memory_only';

/** run 이 인터뷰 가능한 상태인지 (완료/중단/오류로 끝난 실행만 허용). */
export function isInterviewable(run) {
  return INTERVIEWABLE_STATUS.includes(run?.status);
}

// ── API ───────────────────────────────────────────────────────────────────────

function interviewUrl(runId, agentKey) {
  return `/api/simulation/runs/${encodeURIComponent(runId)}`
       + `/agents/${encodeURIComponent(agentKey)}/interview`;
}

/** FastAPI 의 detail(문자열 또는 ValidationError 배열)을 사람이 읽을 문자열로. */
async function toError(res) {
  let detail = '';
  try {
    const data = await res.json();
    if (typeof data?.detail === 'string') {
      detail = data.detail;
    } else if (Array.isArray(data?.detail)) {
      detail = data.detail.map(d => d?.msg || '').filter(Boolean).join(', ');
    }
  } catch (_) { /* 본문이 JSON 이 아니면 무시 */ }
  const err = new Error(detail || `HTTP ${res.status}`);
  err.status = res.status;
  err.detail = detail;
  return err;
}

async function fetchInterviews(runId, agentKey) {
  const res = await fetch(interviewUrl(runId, agentKey));
  if (!res.ok) throw await toError(res);
  return res.json();
}

async function postInterview(runId, agentKey, question, mode) {
  const res = await fetch(interviewUrl(runId, agentKey), {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    // max_tokens 는 보내지 않는다 — 백엔드가 run 설정의 llm_max_tokens 를 따른다.
    body:    JSON.stringify({ question, mode }),
  });
  if (!res.ok) throw await toError(res);
  return res.json();
}

/** HTTP 상태별 안내 문구 + 재시도 유도 여부. */
function friendlyError(err) {
  switch (err.status) {
    case 400: return { text: err.detail || '이 실행은 설정 스냅샷이 없어 인터뷰할 수 없습니다.', retry: false };
    case 404: return { text: '해당 실행 또는 에이전트를 찾을 수 없습니다. 이력이 삭제되었을 수 있습니다.', retry: false };
    case 409: return { text: '시뮬레이션이 아직 진행 중입니다. 실행이 끝난 뒤에 인터뷰할 수 있습니다.', retry: false };
    case 422: return { text: err.detail || '질문을 확인해 주세요. 빈 질문은 보낼 수 없습니다.', retry: false };
    case 502: return { text: 'LLM 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.', retry: true };
    default:
      if (!err.status) return { text: '서버에 연결하지 못했습니다. 네트워크를 확인해 주세요.', retry: true };
      return { text: `인터뷰 요청 실패 (${err.status})${err.detail ? `: ${err.detail}` : ''}`, retry: true };
  }
}

// ── DOM 헬퍼 ──────────────────────────────────────────────────────────────────

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;   // textContent = XSS 안전
  return node;
}

function agentIconOf(agent) {
  return agent ? getAgentIcon(agent) : '🤖';
}

function agentNameOf(agent, fallbackKey) {
  return agent?.display_name || agent?.name || fallbackKey || '';
}

// ── 인터뷰 턴 렌더링 ───────────────────────────────────────────────────────────

/**
 * 질문 말풍선 + 답변 말풍선 한 쌍을 만든다.
 * 답변 노드는 나중에 채우거나 로딩/에러로 바꿀 수 있도록 함께 반환한다.
 */
function buildTurn({ question, mode, createdAt, agent, agentKey }) {
  const turn = el('div', 'sim-itv-turn');

  // ── 질문 (인터뷰어) ──
  const q     = el('div', 'sim-itv-q');
  const qHead = el('div', 'sim-itv-head');
  qHead.appendChild(el('span', 'sim-itv-role', '🎤 인터뷰어'));
  const modeInfo = MODES[mode] || MODES[DEFAULT_MODE];
  qHead.appendChild(el('span', `sim-itv-mode-badge ${mode === 'full_log' ? 'full' : 'memory'}`,
                       `${modeInfo.icon} ${modeInfo.label}`));
  if (createdAt) qHead.appendChild(el('span', 'sim-itv-time', fmtTime(createdAt)));
  q.appendChild(qHead);
  q.appendChild(el('div', 'sim-itv-bubble sim-itv-bubble-q', question));
  turn.appendChild(q);

  // ── 답변 (에이전트) ──
  const a     = el('div', 'sim-itv-a');
  const aHead = el('div', 'sim-itv-head');
  aHead.appendChild(el('span', 'sim-itv-role', `${agentIconOf(agent)} ${agentNameOf(agent, agentKey)}`));
  a.appendChild(aHead);
  const answerBubble = el('div', 'sim-itv-bubble sim-itv-bubble-a');
  a.appendChild(answerBubble);
  turn.appendChild(a);

  return { turn, answerBubble };
}

function setAnswerText(bubble, text) {
  bubble.className = 'sim-itv-bubble sim-itv-bubble-a';
  bubble.textContent = text;
}

function setAnswerLoading(bubble) {
  bubble.className = 'sim-itv-bubble sim-itv-bubble-a sim-itv-loading';
  bubble.textContent = '';
  const dots = el('span', 'sim-itv-dots');
  dots.appendChild(el('i'));
  dots.appendChild(el('i'));
  dots.appendChild(el('i'));
  bubble.appendChild(dots);
  bubble.appendChild(el('span', 'sim-itv-loading-text', '답변을 정리하는 중입니다…'));
}

function setAnswerError(bubble, message, onRetry) {
  bubble.className = 'sim-itv-bubble sim-itv-bubble-a sim-itv-error';
  bubble.textContent = '';
  bubble.appendChild(el('span', 'sim-itv-error-text', `⚠ ${message}`));
  if (onRetry) {
    const btn = el('button', 'sim-itv-retry-btn', '재시도');
    btn.addEventListener('click', onRetry);
    bubble.appendChild(btn);
  }
}

// ── 패널 ──────────────────────────────────────────────────────────────────────

// 인터뷰 패널은 한 번에 하나만 뜬다. 모달 노드를 DOM 에서 지우는 것만으로는
// document 에 걸어 둔 ESC 핸들러가 남으므로(열고 닫기를 반복하면 누적) 모듈
// 스코프에 현재 핸들러를 들고 있다가 새로 열거나 닫을 때 반드시 해제한다.
let activeEscHandler = null;

function detachEsc() {
  if (activeEscHandler) {
    document.removeEventListener('keydown', activeEscHandler);
    activeEscHandler = null;
  }
}

/**
 * 열려 있는 인터뷰 패널을 닫는다(없으면 no-op).
 * 리플레이 모달이 닫히거나 다른 run 으로 갈아탈 때 호출해, 이전 run_id 를
 * 가리키는 패널이 화면에 남지 않게 한다.
 */
export function closeInterviewPanel() {
  detachEsc();
  document.getElementById('sim-interview-modal')?.remove();
}

/**
 * 인터뷰 패널(모달)을 연다. 리플레이 모달 위에 겹쳐 뜬다.
 *
 * @param {string} runId       - 대상 run_id
 * @param {number|string} runNum - 표시용 실행 번호
 * @param {object} run         - GET /api/simulation/runs/{id} 응답
 * @param {Array}  agents      - run 의 config 스냅샷 agents (name/display_name/icon…)
 */
export function openInterviewPanel(runId, runNum, run, agents) {
  const agentList = Array.isArray(agents) ? agents.filter(a => a && a.name) : [];
  if (!agentList.length) {
    alert('이 실행에는 인터뷰할 수 있는 에이전트 정보가 없습니다.');
    return;
  }
  if (!isInterviewable(run)) {
    alert('인터뷰는 종료된 시뮬레이션에서만 가능합니다.');
    return;
  }

  // 이전 패널이 남아 있으면 노드와 ESC 핸들러를 함께 걷어낸다.
  closeInterviewPanel();

  let currentKey  = agentList[0].name;
  let currentMode = DEFAULT_MODE;
  let sending     = false;

  const modal = document.createElement('div');
  modal.id = 'sim-interview-modal';
  modal.className = 'sim-replay-modal-overlay sim-itv-overlay';

  const options = agentList.map(a => {
    const label = `${agentIconOf(a)} ${agentNameOf(a)}`;
    return `<option value="${esc(a.name)}">${esc(label)}</option>`;
  }).join('');

  const modeBtns = Object.entries(MODES).map(([key, m]) => `
    <button type="button" class="sim-itv-mode-btn${key === DEFAULT_MODE ? ' active' : ''}"
            data-mode="${key}" role="radio" aria-checked="${key === DEFAULT_MODE}">
      ${m.icon} ${esc(m.label)}
    </button>`).join('');

  modal.innerHTML = `
    <div class="sim-replay-modal-box sim-itv-box">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span class="sim-itv-title-badge">🎤 인터뷰</span>
          <span class="sim-replay-run-badge">#${esc(runNum)}</span>
          <span>${esc(run.scenario_name || '직접 실행')}</span>
          <span class="sim-replay-meta">시뮬레이션 로그와 별도로 저장됩니다</span>
        </div>
        <div class="sim-replay-actions">
          <button id="sim-itv-close-btn" class="sim-ctrl-btn settings"
                  style="font-size:12px;padding:4px 10px">✕ 닫기</button>
        </div>
      </div>

      <div class="sim-itv-controls">
        <label class="sim-itv-ctrl-label" for="sim-itv-agent-select">대상</label>
        <select id="sim-itv-agent-select" class="sim-itv-agent-select">${options}</select>
        <div class="sim-itv-modes" role="radiogroup" aria-label="인터뷰 컨텍스트 모드">${modeBtns}</div>
      </div>
      <div class="sim-itv-mode-hint" id="sim-itv-mode-hint">${esc(MODES[DEFAULT_MODE].hint)}</div>

      <div class="sim-itv-thread" id="sim-itv-thread"></div>

      <div class="sim-itv-composer">
        <textarea id="sim-itv-question" class="sim-itv-question" rows="2"
                  placeholder="예) 그때 왜 그런 선택을 했나요?  (Ctrl+Enter 로 전송)"></textarea>
        <button id="sim-itv-send-btn" class="sim-ctrl-btn continue sim-itv-send-btn">전송</button>
      </div>
    </div>`;

  document.body.appendChild(modal);

  const threadEl   = modal.querySelector('#sim-itv-thread');
  const selectEl   = modal.querySelector('#sim-itv-agent-select');
  const hintEl     = modal.querySelector('#sim-itv-mode-hint');
  const questionEl = modal.querySelector('#sim-itv-question');
  const sendBtn    = modal.querySelector('#sim-itv-send-btn');

  const agentOf = key => agentList.find(a => a.name === key);
  const scrollToBottom = () => { threadEl.scrollTop = threadEl.scrollHeight; };

  function setSending(on) {
    sending = on;
    sendBtn.disabled   = on;
    selectEl.disabled  = on;
    questionEl.disabled = on;
    sendBtn.textContent = on ? '답변 대기 중…' : '전송';
    modal.querySelectorAll('.sim-itv-mode-btn').forEach(b => { b.disabled = on; });
  }

  function showThreadNotice(text) {
    threadEl.textContent = '';
    threadEl.appendChild(el('div', 'sim-feed-empty-msg', text));
  }

  // ── 기존 인터뷰 기록 로드 ──
  // 에이전트를 빠르게 바꾸면 응답이 뒤바뀔 수 있어 세대(generation) 토큰으로 막는다.
  // 같은 토큰으로 GET↔POST 경쟁도 막는다: 질문을 보내기 시작하면 invalidateLoad()
  // 가 세대를 올려, 아직 끝나지 않은 GET 의 렌더(= threadEl 비우기)가 방금 화면에
  // 붙인 턴을 지우지 못하게 한다.
  let loadSeq = 0;

  /** 진행 중인 loadThread() 의 렌더를 무효화한다. */
  function invalidateLoad() { loadSeq++; }

  async function loadThread() {
    if (sending) return;           // 답변 대기 중에는 스레드를 갈아엎지 않는다
    const seq = ++loadSeq;
    const key = currentKey;
    showThreadNotice('불러오는 중…');
    let records;
    try {
      records = await fetchInterviews(runId, key);
    } catch (e) {
      if (seq !== loadSeq) return;
      showThreadNotice(`기록을 불러오지 못했습니다 — ${friendlyError(e).text}`);
      return;
    }
    if (seq !== loadSeq) return;   // 그 사이 다른 에이전트를 선택함
    threadEl.textContent = '';
    if (!records.length) {
      threadEl.appendChild(el('div', 'sim-feed-empty-msg',
        '아직 인터뷰 기록이 없습니다. 시뮬레이션이 끝난 뒤의 심경을 물어보세요.'));
      return;
    }
    const agent = agentOf(key);
    records.forEach(rec => {
      const { turn, answerBubble } = buildTurn({
        question:  rec.question,
        mode:      rec.mode,
        createdAt: rec.created_at,
        agent,
        agentKey:  rec.agent_key,
      });
      setAnswerText(answerBubble, rec.answer);
      threadEl.appendChild(turn);
    });
    scrollToBottom();
  }

  // ── 질문 전송 ──
  // agentKey/mode 는 전송 시점 값을 고정한다 — 재시도 시 그 사이 바뀐 선택이 아니라
  // 원래 질문을 던졌던 대상/모드로 다시 보내기 위함.
  async function ask(question, mode, agentKey, existing) {
    let turn, answerBubble;

    if (existing) {
      ({ turn, answerBubble } = existing);
      // 재시도인데 그 사이 다른 에이전트를 골랐다면, 요청 대상과 화면이 어긋나므로
      // 조용히 끝내지 말고 이유를 버블에 남긴다(재시도 버튼이 먹통으로 보이는 문제).
      if (currentKey !== agentKey) {
        setAnswerError(answerBubble,
          '대상이 바뀌어 재시도할 수 없습니다. 같은 에이전트를 다시 선택한 뒤 질문해 주세요.', null);
        return;
      }
    }

    // 아직 끝나지 않은 GET 이 있으면 그 렌더를 버린다. 그러지 않으면 아래에서
    // 화면에 붙이는 턴을 loadThread() 의 threadEl 비우기가 삭제해 버린다(M-2).
    // 대상이 같음을 확인한 뒤에 호출해야 남의 로드를 취소하지 않는다.
    invalidateLoad();

    if (existing) {
      // 스레드가 다시 그려지면서 이 턴이 DOM 에서 떨어져 나갔으면 되붙인다.
      if (!turn.isConnected) {
        threadEl.querySelector('.sim-feed-empty-msg')?.remove();
        threadEl.appendChild(turn);
      }
    } else {
      // 첫 질문이면 빈 상태 안내를 치운다.
      threadEl.querySelector('.sim-feed-empty-msg')?.remove();
      ({ turn, answerBubble } = buildTurn({
        question, mode, createdAt: null, agent: agentOf(agentKey), agentKey,
      }));
      threadEl.appendChild(turn);
    }

    setAnswerLoading(answerBubble);
    scrollToBottom();
    setSending(true);

    try {
      const rec = await postInterview(runId, agentKey, question, mode);
      setAnswerText(answerBubble, rec.answer);
      // 서버가 확정한 시각을 헤더에 반영
      const qHead = turn.querySelector('.sim-itv-q .sim-itv-head');
      if (qHead && !qHead.querySelector('.sim-itv-time') && rec.created_at) {
        qHead.appendChild(el('span', 'sim-itv-time', fmtTime(rec.created_at)));
      }
    } catch (e) {
      const { text, retry } = friendlyError(e);
      setAnswerError(answerBubble, text,
        retry ? () => ask(question, mode, agentKey, { turn, answerBubble }) : null);
    } finally {
      setSending(false);
      scrollToBottom();
    }
  }

  function submit() {
    if (sending) return;
    const question = questionEl.value.trim();
    if (!question) { questionEl.focus(); return; }
    questionEl.value = '';
    // 진행 중인 GET 무효화는 ask() 초입에서 처리한다(재시도 경로도 함께 보호).
    ask(question, currentMode, currentKey, null);
  }

  // ── 이벤트 바인딩 ──
  sendBtn.addEventListener('click', submit);
  questionEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); }
  });

  selectEl.addEventListener('change', () => {
    currentKey = selectEl.value;
    loadThread();
  });

  modal.querySelectorAll('.sim-itv-mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (sending) return;
      currentMode = btn.dataset.mode;
      modal.querySelectorAll('.sim-itv-mode-btn').forEach(b => {
        const on = b === btn;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', String(on));
      });
      hintEl.textContent = (MODES[currentMode] || MODES[DEFAULT_MODE]).hint;
    });
  });

  function close() {
    // 이 패널의 핸들러가 아직 활성인 경우에만 해제 — 그 사이 다른 패널이
    // 열렸다면 그쪽 핸들러를 실수로 떼지 않는다.
    if (activeEscHandler === onKeydown) detachEsc();
    else document.removeEventListener('keydown', onKeydown);
    modal.remove();
  }
  function onKeydown(e) {
    // 전송 중에는 실수로 닫히지 않게 ESC 무시
    if (e.key === 'Escape' && !sending) close();
  }
  document.addEventListener('keydown', onKeydown);
  activeEscHandler = onKeydown;
  modal.querySelector('#sim-itv-close-btn').addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal && !sending) close(); });

  loadThread();
  questionEl.focus();
}
