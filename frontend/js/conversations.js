import { state } from './state.js';
import { api } from './api.js';
import { esc, scrollToBottom } from './utils.js';
import { appendMessage } from './messages.js';
import { updateContextBar } from './context-bar.js';
import { refreshThinkingBtn } from './servers.js';

// ── Conversation list ────────────────────────────────────────────────────────

export async function loadConversations() {
  let list;
  try { list = await api('GET', '/conversations'); }
  catch { return; }

  const el = document.getElementById('conversation-list');
  el.innerHTML = '';
  list.forEach(conv => {
    const item = document.createElement('div');
    item.className = 'conv-item' + (conv.id === state.currentConvId ? ' active' : '');
    item.dataset.id = conv.id;
    item.innerHTML = `
      <div class="conv-title">${esc(conv.title)}</div>
      <div class="conv-preview">${esc(conv.last_msg || '—')}</div>
      <button class="conv-delete" title="삭제">✕</button>
    `;
    item.querySelector('.conv-delete').onclick = async e => {
      e.stopPropagation();
      if (!confirm(`"${conv.title}" 대화를 삭제할까요?`)) return;
      await api('DELETE', `/conversations/${conv.id}`);
      if (state.currentConvId === conv.id) {
        state.currentConvId = null;
        document.getElementById('chat-title').textContent = '대화를 선택하거나 새로 시작하세요';
        document.getElementById('messages').innerHTML = `
          <div id="empty-state">
            <div class="icon">✦</div>
            <p>새 대화를 시작하거나 기존 대화를 선택하세요</p>
          </div>`;
        document.getElementById('send-btn').disabled = true;
        // 대화가 사라졌으니 사고 수준도 서버 기본값으로 되돌린다 (버튼은 자동 비활성).
        state.thinkingLevel = state.currentServerThinkingLevel;
        refreshThinkingBtn();
        document.getElementById('websearch-btn').disabled = true;
        state.webSearchEnabled = false;
        document.getElementById('websearch-btn').classList.remove('active');
        document.getElementById('websearch-btn').title = '웹 검색 켜기';
        updateContextBar(null);
      }
      await loadConversations();
    };
    item.onclick = () => openConversation(conv.id);
    el.appendChild(item);
  });
}

// ── Open conversation ─────────────────────────────────────────────────────────

export async function openConversation(id) {
  state.currentConvId = id;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('websearch-btn').disabled = false;
  refreshThinkingBtn();
  document.querySelectorAll('.conv-item').forEach(el =>
    el.classList.toggle('active', el.dataset.id === id)
  );

  let data;
  try { data = await api('GET', `/conversations/${id}`); }
  catch { return; }

  document.getElementById('chat-title').textContent = data.title;

  const messagesEl = document.getElementById('messages');
  messagesEl.innerHTML = '';

  data.turns.forEach(turn => {
    const memories = turn.memories_json ? JSON.parse(turn.memories_json) : [];
    const sources  = turn.sources_json ? JSON.parse(turn.sources_json) : [];
    appendMessage(turn.role, turn.content, memories, {
      context_pct: turn.context_pct,
      prompt_tokens: turn.prompt_tokens,
      max_model_len: turn.max_tokens,
      thinking: turn.thinking || '',
    }, null, sources);
  });

  scrollToBottom();
}

// ── New conversation modal ────────────────────────────────────────────────────

export async function showModal() {
  document.getElementById('modal').classList.remove('hidden');
  await loadAgentsForSelect();
  setTimeout(() => document.getElementById('system-prompt-input').focus(), 50);
}

export function hideModal() {
  document.getElementById('modal').classList.add('hidden');
  document.getElementById('system-prompt-input').value = '';
  document.getElementById('agent-select').value = '';
  document.getElementById('router-mode-check').checked = false;
}

export async function loadAgentsForSelect() {
  const select = document.getElementById('agent-select');
  try {
    const list = await api('GET', '/agents');
    select._agents = list;
    select.innerHTML = '<option value="">에이전트 없이 시작</option>' +
      list.map(a => `<option value="${a.id}">${esc(a.icon)} ${esc(a.name)}</option>`).join('');
  } catch {
    select._agents = [];
  }
}

export async function createConversation() {
  const systemPrompt = document.getElementById('system-prompt-input').value.trim();
  const select       = document.getElementById('agent-select');
  const agentId      = select.value || null;
  const routerMode   = document.getElementById('router-mode-check').checked;
  hideModal();
  try {
    const data = await api('POST', '/conversations', {
      system_prompt: systemPrompt,
      title: '새 대화',
      agent_id: agentId,
      router_mode: routerMode,
    });
    await loadConversations();
    await openConversation(data.id);
  } catch (e) {
    alert('대화 생성 실패: ' + e.message);
  }
}

// ── Init (event bindings) ────────────────────────────────────────────────────

export function initConversationEvents() {
  document.getElementById('new-chat-btn').onclick  = showModal;
  document.getElementById('modal-cancel').onclick  = hideModal;
  document.getElementById('modal-create').onclick  = createConversation;

  document.getElementById('modal').addEventListener('click', e => {
    if (e.target === document.getElementById('modal')) hideModal();
  });

  document.getElementById('system-prompt-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.ctrlKey) createConversation();
  });

  document.getElementById('agent-select').addEventListener('change', function () {
    const list  = this._agents || [];
    const agent = list.find(a => a.id === this.value);
    document.getElementById('system-prompt-input').value = agent ? agent.system_prompt : '';
    if (this.value) document.getElementById('router-mode-check').checked = false;
  });

  document.getElementById('router-mode-check').addEventListener('change', function () {
    if (this.checked) {
      document.getElementById('agent-select').value = '';
      document.getElementById('system-prompt-input').value = '';
    }
  });
}
