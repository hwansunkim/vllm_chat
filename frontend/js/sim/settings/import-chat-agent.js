// frontend/js/sim/settings/import-chat-agent.js
// 시뮬레이션 설정 → 👤 에이전트 섹션 → [💬 채팅에서 가져오기]
//
// GET /api/agents 로 채팅 에이전트를 불러와 고르면, 시뮬레이션 AgentConfig 로
// 매핑해 sim.agents 에 새 카드를 추가한다.
//
// 채팅에는 모델 이름 문자열(model)만 있고 시뮬레이션은 서버 인스턴스(server_id)를
// 가리켜서 개념이 다르다. 자동 매칭은 하지 않는다 — 같은 모델을 서빙하는 서버가
// 여럿이면 매칭이 흔들리고 사용자가 의도하지 않은 서버가 조용히 골라지는 문제가
// 있었다. 그냥 시뮬레이션 기본 서버로 가져오고, 특정 서버가 필요하면 카드에서
// 직접 고르게 한다.
//
// 스냅샷 복사다 — 추가된 시뮬레이션 에이전트는 원본 채팅 에이전트와 연결되지 않는다.

import { api } from '../../api.js';
import { chatAgentToSimAgent, uniqueName } from '../../agent-transfer.js';
import { sim, _expandedAgents, getAllGroups } from '../state.js';
import { renderAgentListInConfig, renderStartAgentSelect } from './agents.js';
import { getServerList, invalidateServerList } from './server-list.js';

let _chatAgents  = [];
let _servers     = [];
let _selectedIdx = -1;
let _busy        = false;

const $ = id => document.getElementById(id);

export function initImportChatAgentEvents() {
  $('sim-import-chat-btn')?.addEventListener('click', openImportChatAgentModal);
  $('sim-import-chat-close')?.addEventListener('click', closeImportChatAgentModal);
  $('sim-import-chat-cancel')?.addEventListener('click', closeImportChatAgentModal);
  $('sim-import-chat-confirm')?.addEventListener('click', confirmImport);
  $('sim-import-chat-modal')?.addEventListener('click', e => {
    if (e.target === $('sim-import-chat-modal')) closeImportChatAgentModal();
  });
}

export async function openImportChatAgentModal() {
  $('sim-import-chat-modal').classList.remove('hidden');
  _selectedIdx = -1;
  setStatus('');
  setListMessage('불러오는 중…');
  renderPreview();

  // 서버 모달에서 목록이 바뀌었을 수 있다 — 아래 수동 선택 드롭다운이 최신 목록을 봐야 한다.
  invalidateServerList();
  try {
    const [agents, servers] = await Promise.all([api('GET', '/agents'), getServerList()]);
    _chatAgents = Array.isArray(agents) ? agents : [];
    _servers    = servers;
  } catch (e) {
    _chatAgents = [];
    setListMessage(`불러오기 실패: ${e.message}`);
    return;
  }
  renderChatAgentList();
  renderPreview();
}

export function closeImportChatAgentModal() {
  $('sim-import-chat-modal').classList.add('hidden');
}

function setListMessage(msg) {
  const list = $('sim-import-chat-list');
  list.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'xfer-empty';
  div.textContent = msg;
  list.appendChild(div);
}

function setStatus(msg, isError = false) {
  const el = $('sim-import-chat-status');
  el.textContent = msg;
  el.classList.toggle('xfer-status-error', !!isError);
}

function renderChatAgentList() {
  const list = $('sim-import-chat-list');
  if (!_chatAgents.length) {
    setListMessage('등록된 채팅 에이전트가 없습니다. 상단 에이전트 관리에서 먼저 만들어보세요.');
    return;
  }
  list.innerHTML = '';
  _chatAgents.forEach((a, idx) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'xfer-item' + (idx === _selectedIdx ? ' selected' : '');

    const icon = document.createElement('span');
    icon.className = 'xfer-item-icon';
    icon.textContent = a.icon || '🤖';

    const info = document.createElement('div');
    info.className = 'xfer-item-info';
    const nameEl = document.createElement('div');
    nameEl.className = 'xfer-item-name';
    nameEl.textContent = a.name || '(이름 없음)';
    const descEl = document.createElement('div');
    descEl.className = 'xfer-item-desc';
    descEl.textContent = a.description || (a.system_prompt || '설명 없음').slice(0, 80);
    info.append(nameEl, descEl);

    item.append(icon, info);
    item.addEventListener('click', () => {
      _selectedIdx = idx;
      renderChatAgentList();
      renderPreview();
    });
    list.appendChild(item);
  });
}

/** 현재 선택으로부터 AgentConfig 후보를 만든다 (선택이 없으면 null). */
function buildCandidate() {
  const chat = _chatAgents[_selectedIdx];
  if (!chat) return null;
  return chatAgentToSimAgent(chat, {
    takenNames: sim.agents.map(a => a.name),
  });
}

function renderPreview() {
  const pane = $('sim-import-chat-preview');
  pane.innerHTML = '';
  const candidate = buildCandidate();
  $('sim-import-chat-confirm').disabled = !candidate || _busy;

  if (!candidate) {
    const div = document.createElement('div');
    div.className = 'xfer-empty';
    div.textContent = '왼쪽에서 가져올 채팅 에이전트를 고르세요.';
    pane.appendChild(div);
    return;
  }

  const { agent } = candidate;

  // ── 그룹/위치는 시나리오 지역 ID라, 다른 시나리오에서 가져온 값이 이 시나리오
  // 어디에도 없으면 대화 상대가 아예 없는 채로 고립된다(ABM/simulation/targets.py
  // — 그룹이 비어있으면 전체 노출, 있으면 같은 그룹원만 노출이라 아무도 공유하지
  // 않는 그룹은 실질적으로 "혼자만의 그룹"이 된다). 값 자체는 그대로 복사하되,
  // 고립 위험이 있으면 확정 전에 눈에 띄게 알린다.
  const knownGroups = new Set(getAllGroups());
  const orphanGroups = (agent.groups || []).filter(g => !knownGroups.has(g));
  if (orphanGroups.length) {
    const warn = document.createElement('div');
    warn.className = 'xfer-note xfer-note-warn';
    warn.textContent = `⚠ 그룹 "${orphanGroups.join(', ')}" 은(는) 이 시나리오의 다른 에이전트가 아무도 안 씁니다 — `
      + `이 상태로 시작하면 이 에이전트는 아무와도 대화하지 못합니다. 필요하면 가져온 뒤 그룹을 지우거나 이 시나리오의 기존 그룹으로 바꾸세요.`;
    pane.appendChild(warn);
  }
  const knownLocations = new Set((sim.location_graph || []).map(n => n.name));
  if (agent.location && !knownLocations.has(agent.location)) {
    const warn = document.createElement('div');
    warn.className = 'xfer-note xfer-note-warn';
    warn.textContent = `⚠ 위치 "${agent.location}" 은(는) 이 시나리오의 위치 그래프에 없습니다 — 시작 위치를 다시 지정하는 게 좋습니다.`;
    pane.appendChild(warn);
  }

  // ── 확인/변경 가능한 필드: ID, LLM 서버 ──
  const idField = document.createElement('div');
  idField.className = 'xfer-field';
  const idLabel = document.createElement('label');
  idLabel.setAttribute('for', 'sim-import-chat-name');
  idLabel.textContent = '에이전트 ID *';
  const idInput = document.createElement('input');
  idInput.type = 'text';
  idInput.id = 'sim-import-chat-name';
  idInput.value = agent.name;
  idField.append(idLabel, idInput);
  pane.appendChild(idField);

  const svField = document.createElement('div');
  svField.className = 'xfer-field';
  const svLabel = document.createElement('label');
  svLabel.setAttribute('for', 'sim-import-chat-server');
  svLabel.textContent = 'LLM 서버';
  const svSelect = document.createElement('select');
  svSelect.id = 'sim-import-chat-server';
  const defOpt = document.createElement('option');
  defOpt.value = '';
  defOpt.textContent = '기본 서버 사용';
  svSelect.appendChild(defOpt);
  _servers.filter(s => s.enabled !== false).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.model ? `${s.name} — ${s.model}` : s.name;
    svSelect.appendChild(opt);
  });
  // agent.server_id는 chatAgentToSimAgent()가 항상 null로 채우므로 기본값이
  // 그대로 "기본 서버 사용"이다 — 특정 서버가 필요하면 여기서 직접 고른다.
  svSelect.value = agent.server_id ?? '';
  svField.append(svLabel, svSelect);
  pane.appendChild(svField);

  // ── 그 외 복사되는 값 ──
  const rows = [
    ['아이콘',        agent.icon],
    ['표시이름',       agent.display_name || '(없음)'],
    ['시스템 프롬프트', agent.system_prompt || '(없음)'],
    ['온도',          agent.temperature ?? '(시뮬레이션 기본값)'],
    ['성별',          agent.gender],
    ['그룹',          agent.groups.length ? agent.groups.join(', ') : '(없음)'],
    ['위치',          agent.location || '(없음)'],
    ['외모 묘사',      agent.visual_description || '(없음)'],
    ['처음부터 등장',   agent.initial_active ? '예' : '아니오'],
    ['보존 (ABM 미사용)', [
      agent.role       ? `역할=${agent.role}`       : null,
      agent.goal       ? `목표=${agent.goal}`       : null,
      agent.backstory  ? `배경=${agent.backstory}`  : null,
      agent.model      ? `모델=${agent.model}`      : null,
      agent.max_tokens ? `최대토큰=${agent.max_tokens}` : null,
    ].filter(Boolean).join(' / ') || '(없음)'],
  ];
  const table = document.createElement('dl');
  table.className = 'xfer-fields';
  rows.forEach(([k, v]) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = String(v);
    table.append(dt, dd);
  });
  pane.appendChild(table);

  const tail = document.createElement('div');
  tail.className = 'xfer-hint';
  tail.textContent = '역할·목표·배경·모델·최대 토큰은 ABM 엔진이 쓰지 않지만, 다시 채팅으로 가져갈 때 복원되도록 시나리오에 보존됩니다.';
  pane.appendChild(tail);
}

function confirmImport() {
  const candidate = buildCandidate();
  if (!candidate || _busy) return;

  const agent = { ...candidate.agent };
  const typed = ($('sim-import-chat-name')?.value ?? '').trim();
  if (!typed) { setStatus('에이전트 ID 를 입력하세요.', true); return; }
  // 사용자가 직접 고친 ID 도 기존 카드와 부딪히지 않게 한 번 더 검사한다.
  agent.name      = uniqueName(typed, sim.agents.map(a => a.name));
  agent.server_id = $('sim-import-chat-server')?.value || null;

  sim.agents.push(agent);
  _expandedAgents.add(agent.name);
  renderAgentListInConfig();
  renderStartAgentSelect();
  setStatus('');
  closeImportChatAgentModal();
}
