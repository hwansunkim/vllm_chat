import { state } from './state.js';
import { appendMessage, appendLoadingBubble, removeLoadingBubble } from './messages.js';
import { createStreamRow, readSSEStream } from './stream.js';
import { loadConversations } from './conversations.js';
import { refreshThinkingBtn, initThinkingControl } from './servers.js';
import { scrollToBottom } from './utils.js';

export async function sendMessage() {
  if (!state.currentConvId || state.isSending) return;
  const input   = document.getElementById('message-input');
  const content = input.value.trim();
  if (!content) return;

  state.isSending = true;
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('send-btn').disabled     = true;
  refreshThinkingBtn();   // isSending 이므로 버튼 비활성 + 열린 팝오버 닫힘
  document.getElementById('websearch-btn').disabled = true;

  appendMessage('user', content);
  scrollToBottom();
  appendLoadingBubble();

  try {
    const response = await fetch(`/api/conversations/${state.currentConvId}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        // 항상 명시 전송한다. null 은 "서버 기본값에 맡김" 이라는 별도 의미라
        // 사용자가 고른 'off' 를 null 로 보내면 안 된다.
        thinking_level: state.thinkingLevel,
        web_search: state.webSearchEnabled,
      }),
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
    refreshThinkingBtn();   // 사용자가 고른 수준은 유지하고 활성 상태만 되돌린다
    document.getElementById('websearch-btn').disabled = !state.currentConvId;
  }
}

export function initChatEvents() {
  document.getElementById('send-btn').onclick = sendMessage;

  // 🧠 버튼은 단순 토글이 아니라 4단계 팝오버 메뉴다 (servers.js 가 소유).
  initThinkingControl();

  document.getElementById('websearch-btn').onclick = () => {
    state.webSearchEnabled = !state.webSearchEnabled;
    const btn = document.getElementById('websearch-btn');
    btn.classList.toggle('active', state.webSearchEnabled);
    btn.title = state.webSearchEnabled ? '웹 검색 끄기' : '웹 검색 켜기';
  };

  document.getElementById('message-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  document.getElementById('message-input').addEventListener('input', function () {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  });
}
