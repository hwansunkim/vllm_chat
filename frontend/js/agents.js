import { state } from './state.js';
import { api } from './api.js';
import { esc } from './utils.js';
import { initAgentImportEvents } from './agent-import-modal.js';

let _editAgentId = null;

export async function openAgentModal() {
  document.getElementById('agent-modal').classList.remove('hidden');
  await loadAgents();
}

export function closeAgentModal() {
  document.getElementById('agent-modal').classList.add('hidden');
  hideAgentForm();
}

export async function loadAgents() {
  try {
    state.agentList = await api('GET', '/agents');
    renderAgents();
  } catch (e) {
    document.getElementById('agent-list').innerHTML =
      `<div class="agent-empty">불러오기 실패: ${esc(e.message)}</div>`;
  }
}

function renderAgents() {
  const listEl = document.getElementById('agent-list');
  document.getElementById('agent-count-badge').textContent = state.agentList.length + '개';

  if (!state.agentList.length) {
    listEl.innerHTML = '<div class="agent-empty">등록된 에이전트가 없습니다.<br>+ 새 에이전트 버튼으로 만들어보세요.</div>';
    return;
  }

  listEl.innerHTML = state.agentList.map(a => {
    const promptPreview = a.system_prompt
      ? (a.system_prompt.length > 70 ? a.system_prompt.slice(0, 70) + '...' : a.system_prompt)
      : '시스템 프롬프트 없음';
    const modelInfo = a.model ? ` · ${a.model.split('/').pop()}` : '';
    return `
      <div class="agent-card" data-id="${a.id}">
        <div class="agent-card-header">
          <span class="agent-icon-display">${esc(a.icon)}</span>
          <div class="agent-card-info">
            <div class="agent-card-name">${esc(a.name)}</div>
            <div class="agent-card-desc">${esc(a.description || '설명 없음')}</div>
          </div>
          <div class="agent-card-actions">
            <button class="agent-edit-btn" data-edit="${a.id}">편집</button>
            <button class="agent-delete-btn" data-del="${a.id}">삭제</button>
          </div>
        </div>
        <div class="agent-card-prompt">${esc(promptPreview)}</div>
        <div class="agent-card-meta">온도 ${a.temperature} · 최대 ${Number(a.max_tokens).toLocaleString()}토큰${esc(modelInfo)}</div>
      </div>`;
  }).join('');
}

function showAgentForm(agentId = null) {
  _editAgentId = agentId;
  const agent = agentId ? state.agentList.find(a => a.id === agentId) : null;

  document.getElementById('agent-form-title').textContent = agent ? '에이전트 편집' : '새 에이전트';
  document.getElementById('agent-icon').value      = agent?.icon ?? '🤖';
  document.getElementById('agent-name').value      = agent?.name ?? '';
  document.getElementById('agent-desc').value      = agent?.description ?? '';
  document.getElementById('agent-prompt').value    = agent?.system_prompt ?? '';
  document.getElementById('agent-role').value      = agent?.role ?? '';
  document.getElementById('agent-goal').value      = agent?.goal ?? '';
  document.getElementById('agent-backstory').value = agent?.backstory ?? '';
  document.getElementById('agent-temp').value      = agent?.temperature ?? 0.7;
  document.getElementById('agent-temp-val').textContent = agent?.temperature ?? 0.7;
  document.getElementById('agent-tokens').value    = agent?.max_tokens ?? 1024;
  document.getElementById('agent-model').value     = agent?.model ?? '';

  document.getElementById('agent-list-panel').classList.add('hidden');
  document.getElementById('agent-form-panel').classList.remove('hidden');
  setTimeout(() => document.getElementById('agent-name').focus(), 50);
}

function hideAgentForm() {
  _editAgentId = null;
  document.getElementById('agent-form-panel').classList.add('hidden');
  document.getElementById('agent-list-panel').classList.remove('hidden');
}

async function saveAgent() {
  const name = document.getElementById('agent-name').value.trim();
  if (!name) { alert('에이전트 이름을 입력하세요.'); return; }

  const body = {
    name,
    description:   document.getElementById('agent-desc').value.trim(),
    system_prompt: document.getElementById('agent-prompt').value.trim(),
    icon:          document.getElementById('agent-icon').value.trim() || '🤖',
    temperature:   parseFloat(document.getElementById('agent-temp').value),
    max_tokens:    parseInt(document.getElementById('agent-tokens').value),
    model:         document.getElementById('agent-model').value.trim() || null,
    role:          document.getElementById('agent-role').value.trim(),
    goal:          document.getElementById('agent-goal').value.trim(),
    backstory:     document.getElementById('agent-backstory').value.trim(),
  };

  try {
    if (_editAgentId) await api('PUT',  `/agents/${_editAgentId}`, body);
    else              await api('POST', '/agents', body);
    hideAgentForm();
    await loadAgents();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteAgent(id) {
  const agent = state.agentList.find(a => a.id === id);
  if (!confirm(`"${agent?.name}" 에이전트를 삭제할까요?`)) return;
  try {
    await api('DELETE', `/agents/${id}`);
    await loadAgents();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

export function initAgentEvents() {
  document.getElementById('agent-btn').onclick         = openAgentModal;
  document.getElementById('agent-close').onclick       = closeAgentModal;
  document.getElementById('agent-new-btn').onclick     = () => showAgentForm();
  document.getElementById('agent-form-close').onclick  = hideAgentForm;
  document.getElementById('agent-form-cancel').onclick = hideAgentForm;
  document.getElementById('agent-form-save').onclick   = saveAgent;

  // "🎬 시뮬레이션에서 가져오기" — 성공하면 목록을 다시 읽어 새 카드가 바로 보이게 한다.
  initAgentImportEvents(loadAgents);

  // 편집/삭제 위임 클릭 리스너 — #agent-list 자체는 renderAgents()가 innerHTML만
  // 갈아끼우고 엘리먼트를 교체하지 않으므로, 여기 initAgentEvents()(앱 초기화 시 1회
  // 실행)에서 한 번만 붙인다. 예전엔 renderAgents() 안에 있어서 loadAgents()를 부를
  // 때마다(가져오기 성공마다도 포함) 리스너가 계속 쌓였다.
  document.getElementById('agent-list').addEventListener('click', async e => {
    const editBtn = e.target.closest('[data-edit]');
    const delBtn  = e.target.closest('[data-del]');
    if (editBtn) showAgentForm(editBtn.dataset.edit);
    if (delBtn)  await deleteAgent(delBtn.dataset.del);
  });

  document.getElementById('agent-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('agent-modal')) closeAgentModal();
  });

  document.getElementById('agent-temp').addEventListener('input', function () {
    document.getElementById('agent-temp-val').textContent = this.value;
  });

  document.getElementById('agent-gen-prompt').addEventListener('click', () => {
    const role      = document.getElementById('agent-role').value.trim();
    const goal      = document.getElementById('agent-goal').value.trim();
    const backstory = document.getElementById('agent-backstory').value.trim();
    if (!role && !goal && !backstory) { alert('역할, 목표, 배경 중 하나 이상을 입력하세요.'); return; }
    const parts = [];
    if (role)      parts.push(`당신은 ${role}입니다.`);
    if (goal)      parts.push(`목표: ${goal}`);
    if (backstory) parts.push(`배경: ${backstory}`);
    document.getElementById('agent-prompt').value = parts.join('\n');
  });
}
