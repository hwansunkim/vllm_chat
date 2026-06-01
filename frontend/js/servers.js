import { state } from './state.js';
import { api } from './api.js';
import { esc } from './utils.js';

let _statusServers = [];

// ── Model status + Thinking button ──────────────────────────────────────────

export async function loadModelStatus() {
  try {
    const data = await api('GET', '/model/status');
    _statusServers = data.servers || [];

    const name = data.model ? data.model.split('/').pop() : '없음';
    document.getElementById('model-badge').textContent =
      name + (data.max_model_len ? ` · ${(data.max_model_len / 1000).toFixed(0)}K ctx` : '');

    const serverName = data.current_server?.name ?? '서버 없음';
    document.getElementById('current-server-name').textContent = serverName;

    updateThinkingBtn(data.current_server?.thinking ?? false);
    renderServerDropdown(_statusServers);
  } catch {
    document.getElementById('model-badge').textContent = '연결 오류';
    document.getElementById('current-server-name').textContent = '연결 오류';
  }
}

export function updateThinkingBtn(supportsThinking) {
  state.currentServerThinking = supportsThinking;
  const btn = document.getElementById('thinking-btn');
  if (!state.currentConvId) return;

  if (!supportsThinking) {
    if (state.thinkingEnabled) {
      state.thinkingEnabled = false;
      btn.classList.remove('active');
    }
    btn.disabled = true;
    btn.title = '이 서버는 Thinking 모드를 지원하지 않습니다';
  } else {
    btn.disabled = false;
    btn.title = state.thinkingEnabled ? '사고 모드 끄기' : '사고 모드 켜기';
  }
}

// ── Server dropdown ──────────────────────────────────────────────────────────

function renderServerDropdown(servers) {
  const listEl = document.getElementById('server-dropdown-list');
  if (!servers.length) {
    listEl.innerHTML = '<div style="padding:12px 14px;font-size:13px;color:#94a3b8;">등록된 서버 없음</div>';
    return;
  }
  listEl.innerHTML = servers.map(s => {
    const isActive  = s.is_default;
    const modelShort = s.model.split('/').pop();
    const ctxInfo   = s.model_len ? ` · ${(s.model_len / 1000).toFixed(0)}K ctx` : '';
    const thinkTag  = s.thinking ? ' <span style="font-size:10px;color:#d97706">🧠</span>' : '';
    return `
      <div class="server-dropdown-item ${isActive ? 'active' : ''}" data-id="${s.id}">
        <span class="ss-check">${isActive ? '✓' : ''}</span>
        <div class="ss-info">
          <div class="ss-name">${esc(s.name)}${thinkTag}</div>
          <div class="ss-model">${esc(modelShort)}${esc(ctxInfo)}</div>
        </div>
      </div>`;
  }).join('');

  listEl.addEventListener('click', async e => {
    const item = e.target.closest('.server-dropdown-item');
    if (item) await switchDefaultServer(item.dataset.id);
  });
}

async function switchDefaultServer(serverId) {
  if (_statusServers.find(s => s.id === serverId)?.is_default) return;
  document.querySelectorAll('.server-dropdown-item').forEach(el => el.classList.add('switching'));
  try {
    await api('PUT', `/servers/${serverId}`, { is_default: true });
    closeServerDropdown();
    await loadModelStatus();
  } catch (e) {
    alert('서버 전환 실패: ' + e.message);
    document.querySelectorAll('.server-dropdown-item').forEach(el => el.classList.remove('switching'));
  }
}

function toggleServerDropdown() {
  const dd  = document.getElementById('server-dropdown');
  const btn = document.getElementById('server-selector-btn');
  if (!dd.classList.contains('hidden')) {
    closeServerDropdown();
  } else {
    dd.classList.remove('hidden');
    btn.classList.add('open');
  }
}

function closeServerDropdown() {
  document.getElementById('server-dropdown').classList.add('hidden');
  document.getElementById('server-selector-btn').classList.remove('open');
}

// ── Server modal ─────────────────────────────────────────────────────────────

let _serverList   = [];
let _editServerId = null;

export async function openServerModal() {
  document.getElementById('server-modal').classList.remove('hidden');
  await loadServers();
}

function closeServerModal() {
  document.getElementById('server-modal').classList.add('hidden');
  hideServerForm();
}

async function loadServers() {
  try {
    _serverList = await api('GET', '/servers');
    renderServers();
  } catch (e) {
    document.getElementById('server-list').innerHTML =
      `<div class="server-empty">불러오기 실패: ${esc(e.message)}</div>`;
  }
}

function renderServers() {
  const listEl = document.getElementById('server-list');
  document.getElementById('server-count-badge').textContent = _serverList.length + '개';

  if (!_serverList.length) {
    listEl.innerHTML = '<div class="server-empty">등록된 서버가 없습니다.<br>+ 새 서버 버튼으로 추가하세요.</div>';
    return;
  }

  listEl.innerHTML = _serverList.map(s => {
    const ctxInfo       = s.model_len ? ` · ${(s.model_len / 1000).toFixed(0)}K ctx` : '';
    const modelShort    = s.model.split('/').pop();
    const defaultBadge  = s.is_default ? '<span class="server-default-badge">기본</span>' : '';
    const thinkingBadge = s.thinking   ? '<span class="server-thinking-badge">Thinking</span>' : '';
    const statusBadge   = s.enabled
      ? '<span class="server-status enabled">활성</span>'
      : '<span class="server-status disabled">비활성</span>';
    return `
      <div class="server-card" data-id="${s.id}">
        <div class="server-card-top">
          <div class="server-card-info">
            <div class="server-card-name">${esc(s.name)} ${defaultBadge} ${thinkingBadge} ${statusBadge}</div>
            <div class="server-card-url">${esc(s.base_url)}</div>
            <div class="server-card-model">${esc(modelShort)}${esc(ctxInfo)}</div>
          </div>
          <div class="server-card-actions">
            <button class="server-health-btn" data-id="${s.id}">● 연결 확인</button>
            <button class="agent-edit-btn" data-edit="${s.id}">편집</button>
            <button class="agent-delete-btn" data-del="${s.id}">삭제</button>
          </div>
        </div>
      </div>`;
  }).join('');

  listEl.addEventListener('click', async e => {
    const healthBtn = e.target.closest('.server-health-btn');
    const editBtn   = e.target.closest('[data-edit]');
    const delBtn    = e.target.closest('[data-del]');
    if (healthBtn) await checkServerHealth(healthBtn.dataset.id, healthBtn);
    if (editBtn)   showServerForm(editBtn.dataset.edit);
    if (delBtn)    await deleteServer(delBtn.dataset.del);
  });
}

async function checkServerHealth(serverId, btn) {
  btn.textContent = '● 확인 중...';
  btn.disabled = true;
  btn.className = 'server-health-btn';
  try {
    const data = await api('GET', `/servers/${serverId}/health`);
    btn.textContent = data.healthy ? '🟢 정상' : '🔴 오류';
    btn.className   = 'server-health-btn ' + (data.healthy ? 'healthy' : 'unhealthy');
  } catch {
    btn.textContent = '🔴 오류';
    btn.className   = 'server-health-btn unhealthy';
  } finally {
    btn.disabled = false;
  }
}

function showServerForm(serverId = null) {
  _editServerId = serverId;
  const server = serverId ? _serverList.find(s => s.id === serverId) : null;

  document.getElementById('server-form-title').textContent = server ? '서버 편집' : '새 서버';
  document.getElementById('sv-name').value    = server?.name ?? '';
  document.getElementById('sv-url').value     = server?.base_url ?? '';
  document.getElementById('sv-model').value   = server?.model ?? '';
  document.getElementById('sv-weight').value  = server?.weight ?? 1;
  document.getElementById('sv-api-key').value  = server?.api_key ?? '';
  document.getElementById('sv-max-len').value   = server?.max_model_len ?? 0;
  document.getElementById('sv-default').checked  = server?.is_default ?? false;
  document.getElementById('sv-enabled').checked  = server?.enabled ?? true;
  document.getElementById('sv-thinking').checked = server?.thinking ?? false;
  document.getElementById('sv-enabled-row').style.display = serverId ? 'flex' : 'none';

  document.getElementById('server-list-panel').classList.add('hidden');
  document.getElementById('server-form-panel').classList.remove('hidden');
  setTimeout(() => document.getElementById('sv-name').focus(), 50);
}

function hideServerForm() {
  _editServerId = null;
  document.getElementById('server-form-panel')?.classList.add('hidden');
  document.getElementById('server-list-panel')?.classList.remove('hidden');
}

async function saveServer() {
  const name     = document.getElementById('sv-name').value.trim();
  const base_url = document.getElementById('sv-url').value.trim();
  const model    = document.getElementById('sv-model').value.trim();

  if (!name || !base_url || !model) {
    alert('이름, URL, 모델은 필수 항목입니다.');
    return;
  }

  const body = {
    name, base_url, model,
    api_key:       document.getElementById('sv-api-key').value.trim(),
    weight:        parseInt(document.getElementById('sv-weight').value) || 1,
    max_model_len: parseInt(document.getElementById('sv-max-len').value) || 0,
    is_default:    document.getElementById('sv-default').checked,
    thinking:      document.getElementById('sv-thinking').checked,
  };
  if (_editServerId) body.enabled = document.getElementById('sv-enabled').checked;

  try {
    if (_editServerId) await api('PUT',  `/servers/${_editServerId}`, body);
    else               await api('POST', '/servers', body);
    hideServerForm();
    await loadServers();
    await loadModelStatus();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteServer(id) {
  const server = _serverList.find(s => s.id === id);
  if (!confirm(`"${server?.name}" 서버를 삭제할까요?`)) return;
  try {
    await api('DELETE', `/servers/${id}`);
    await loadServers();
    await loadModelStatus();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ── Init (event bindings) ────────────────────────────────────────────────────

export function initServerEvents() {
  document.getElementById('server-selector-btn').addEventListener('click', e => {
    e.stopPropagation();
    toggleServerDropdown();
  });
  document.addEventListener('click', e => {
    if (!document.getElementById('server-selector').contains(e.target)) {
      closeServerDropdown();
    }
  });

  document.getElementById('server-btn').onclick        = openServerModal;
  document.getElementById('server-close').onclick      = closeServerModal;
  document.getElementById('server-new-btn').onclick    = () => showServerForm();
  document.getElementById('server-form-close').onclick = hideServerForm;
  document.getElementById('server-form-cancel').onclick = hideServerForm;
  document.getElementById('server-form-save').onclick  = saveServer;

  document.getElementById('server-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('server-modal')) closeServerModal();
  });
}
