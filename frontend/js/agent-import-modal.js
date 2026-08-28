// frontend/js/agent-import-modal.js
// "에이전트 관리" 모달 → [🎬 시뮬레이션에서 가져오기]
//
// 저장된 시뮬레이션 시나리오를 고르고 → 그 안의 에이전트를 고르면 → 채팅 에이전트
// 스키마로 매핑한 미리보기를 보여주고 → 확인 시 POST /api/agents 로 새로 만든다.
//
// 스냅샷 복사다. 만들어진 채팅 에이전트는 원본 시나리오와 아무 연결도 갖지 않는다.

import { api } from './api.js';
import { fetchScenarioList } from './sim/scenarios.js';
import { getServerList, invalidateServerList } from './sim/settings/server-list.js';
import { simAgentToChatBody, describeModelSource } from './agent-transfer.js';

let _scenarios   = [];   // 저장된 시나리오 전체 (config 포함)
let _servers     = [];   // GET /api/servers 캐시
let _selectedIdx = -1;   // 현재 시나리오 안에서 고른 에이전트 인덱스
let _onImported  = null; // 가져오기 성공 후 호출 (채팅 에이전트 목록 새로고침)
let _busy        = false;

const $ = id => document.getElementById(id);

export function initAgentImportEvents(onImported) {
  _onImported = onImported;
  $('agent-import-open-btn').onclick = openAgentImportModal;
  $('agent-import-close').onclick    = closeAgentImportModal;
  $('agent-import-cancel').onclick   = closeAgentImportModal;
  $('agent-import-confirm').onclick  = confirmImport;
  $('agent-import-scenario').addEventListener('change', () => {
    _selectedIdx = -1;
    renderSimAgentList();
    renderPreview();
  });
  $('agent-import-modal').addEventListener('click', e => {
    if (e.target === $('agent-import-modal')) closeAgentImportModal();
  });
}

export async function openAgentImportModal() {
  $('agent-import-modal').classList.remove('hidden');
  _selectedIdx = -1;
  setStatus('');
  setListMessage('불러오는 중…');
  renderPreview();

  // 서버 모달에서 서버가 추가/삭제됐을 수 있으므로 캐시를 버리고 새로 읽는다 —
  // server_id → 모델명 역참조가 최신 목록을 봐야 한다.
  invalidateServerList();
  try {
    const [scenarios, servers] = await Promise.all([fetchScenarioList(), getServerList()]);
    _scenarios = scenarios;
    _servers   = servers;
  } catch (e) {
    _scenarios = [];
    setListMessage(`불러오기 실패: ${e.message}`);
    fillScenarioSelect();
    return;
  }
  fillScenarioSelect();
  renderSimAgentList();
  renderPreview();
}

export function closeAgentImportModal() {
  $('agent-import-modal').classList.add('hidden');
}

function fillScenarioSelect() {
  const sel = $('agent-import-scenario');
  sel.innerHTML = '';
  if (!_scenarios.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '저장된 시나리오가 없습니다';
    sel.appendChild(opt);
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  _scenarios.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    sel.appendChild(opt);
  });
}

function currentScenario() {
  const id = $('agent-import-scenario').value;
  return _scenarios.find(s => s.id === id) || null;
}

function currentAgents() {
  const sc = currentScenario();
  const list = sc?.config?.agents;
  return Array.isArray(list) ? list : [];
}

function setListMessage(msg) {
  const list = $('agent-import-agent-list');
  list.innerHTML = '';
  const div = document.createElement('div');
  div.className = 'xfer-empty';
  div.textContent = msg;
  list.appendChild(div);
}

function setStatus(msg, isError = false) {
  const el = $('agent-import-status');
  el.textContent = msg;
  el.classList.toggle('xfer-status-error', !!isError);
}

function renderSimAgentList() {
  const list   = $('agent-import-agent-list');
  const agents = currentAgents();

  if (!_scenarios.length) { setListMessage('저장된 시나리오가 없습니다. 시뮬레이션 화면에서 먼저 시나리오를 저장하세요.'); return; }
  if (!agents.length)     { setListMessage('이 시나리오에는 에이전트가 없습니다.'); return; }

  list.innerHTML = '';
  agents.forEach((a, idx) => {
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
    nameEl.textContent = a.display_name ? `${a.display_name} (${a.name})` : (a.name || '(이름 없음)');
    const descEl = document.createElement('div');
    descEl.className = 'xfer-item-desc';
    descEl.textContent = (a.system_prompt || '시스템 프롬프트 없음').slice(0, 80);
    info.append(nameEl, descEl);

    item.append(icon, info);
    item.addEventListener('click', () => {
      _selectedIdx = idx;
      renderSimAgentList();
      renderPreview();
    });
    list.appendChild(item);
  });
}

/** 현재 선택으로부터 POST 본문 후보를 만든다 (선택이 없으면 null). */
function buildCandidate() {
  const agents = currentAgents();
  const agent  = agents[_selectedIdx];
  if (!agent) return null;
  return simAgentToChatBody(agent, {
    servers: _servers,
    fallbackTemperature: currentScenario()?.config?.temperature,
  });
}

function renderPreview() {
  const pane = $('agent-import-preview');
  pane.innerHTML = '';
  const candidate = buildCandidate();
  $('agent-import-confirm').disabled = !candidate || _busy;

  if (!candidate) {
    const div = document.createElement('div');
    div.className = 'xfer-empty';
    div.textContent = '왼쪽에서 가져올 에이전트를 고르세요.';
    pane.appendChild(div);
    return;
  }

  const { body, server, modelSource } = candidate;

  // ── 모델 출처 안내 ──
  const note = document.createElement('div');
  note.className = 'xfer-note' + (modelSource === 'none' ? ' xfer-note-warn' : '');
  note.textContent = describeModelSource(modelSource, server, body.model || '');
  pane.appendChild(note);

  // ── 편집 가능한 필드: 이름 / 모델 ──
  pane.appendChild(makeInput('agent-import-name',  '이름 *',  body.name,        '채팅 에이전트 이름'));
  pane.appendChild(makeInput('agent-import-model', '모델',    body.model || '', '비우면 기본 모델 사용'));

  // ── 그 외 복사되는 값 (읽기 전용 요약) ──
  const rows = [
    ['아이콘',         body.icon],
    ['시스템 프롬프트', body.system_prompt || '(없음)'],
    ['역할 / 목표 / 배경', [body.role, body.goal, body.backstory].filter(Boolean).join(' / ') || '(없음)'],
    ['온도 / 최대 토큰', `${body.temperature} / ${body.max_tokens}`],
    ['표시이름',        body.display_name || '(없음)'],
    ['성별',           body.gender],
    ['그룹',           body.groups.length ? body.groups.join(', ') : '(없음)'],
    ['위치',           body.location || '(없음)'],
    ['외모 묘사',       body.visual_description || '(없음)'],
    ['처음부터 등장',    body.initial_active ? '예' : '아니오'],
  ];
  const table = document.createElement('dl');
  table.className = 'xfer-fields';
  rows.forEach(([k, v]) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    table.append(dt, dd);
  });
  pane.appendChild(table);

  const tail = document.createElement('div');
  tail.className = 'xfer-hint';
  tail.textContent = '성별·그룹·위치·외모 묘사·표시이름·처음부터 등장은 채팅 편집 폼에 입력란이 없지만 값은 그대로 보존됩니다.';
  pane.appendChild(tail);
}

function makeInput(id, label, value, placeholder) {
  const wrap = document.createElement('div');
  wrap.className = 'xfer-field';
  const lab = document.createElement('label');
  lab.setAttribute('for', id);
  lab.textContent = label;
  const input = document.createElement('input');
  input.type = 'text';
  input.id = id;
  input.value = value ?? '';
  input.placeholder = placeholder || '';
  wrap.append(lab, input);
  return wrap;
}

async function confirmImport() {
  const candidate = buildCandidate();
  if (!candidate || _busy) return;

  const body  = { ...candidate.body };
  body.name   = ($('agent-import-name')?.value ?? body.name).trim();
  const model = ($('agent-import-model')?.value ?? '').trim();
  body.model  = model || null;

  if (!body.name) { setStatus('에이전트 이름을 입력하세요.', true); return; }

  _busy = true;
  $('agent-import-confirm').disabled = true;
  setStatus('가져오는 중…');
  try {
    await api('POST', '/agents', body);
    setStatus('');
    closeAgentImportModal();
    await _onImported?.();
  } catch (e) {
    setStatus(`가져오기 실패: ${e.message}`, true);
  } finally {
    _busy = false;
    $('agent-import-confirm').disabled = false;
  }
}
