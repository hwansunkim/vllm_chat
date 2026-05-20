import { state } from './state.js';
import { appendMessage, appendLoadingBubble, removeLoadingBubble } from './messages.js';
import { createStreamRow, readSSEStream } from './stream.js';
import { loadConversations } from './conversations.js';
import { updateThinkingBtn } from './servers.js';
import { scrollToBottom } from './utils.js';

export async function sendMessage() {
  if (!state.currentConvId || state.isSending) return;
  const input   = document.getElementById('message-input');
  const content = input.value.trim();
  if (!content) return;

  state.isSending = true;
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('send-btn').disabled    = true;
  document.getElementById('thinking-btn').disabled = true;

  appendMessage('user', content);
  scrollToBottom();
  appendLoadingBubble();

  try {
    const response = await fetch(`/api/conversations/${state.currentConvId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, thinking: state.thinkingEnabled }),
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `HTTP ${response.status}`);
    }

    removeLoadingBubble();
    const row = createStreamRow();
    await readSSEStream(response.body, row);

    scrollToBottom();
    await loadConversations();
  } catch (e) {
    removeLoadingBubble();
    appendMessage('assistant', `오류가 발생했습니다: ${e.message}`);
  } finally {
    state.isSending = false;
    document.getElementById('send-btn').disabled = false;
    updateThinkingBtn(state.currentServerThinking);
  }
}

export function initChatEvents() {
  document.getElementById('send-btn').onclick = sendMessage;

  document.getElementById('thinking-btn').onclick = () => {
    state.thinkingEnabled = !state.thinkingEnabled;
    const btn = document.getElementById('thinking-btn');
    btn.classList.toggle('active', state.thinkingEnabled);
    btn.title = state.thinkingEnabled ? '사고 모드 끄기' : '사고 모드 켜기';
  };

  document.getElementById('message-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  document.getElementById('message-input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
}
