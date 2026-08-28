import { state } from './state.js';
import { esc, scrollToBottom, removeEmptyState } from './utils.js';
import { renderMarkdown, highlightCodeBlocks } from './markdown.js';
import { updateContextBar } from './context-bar.js';
import { renderSourceRefs } from './sources.js';

export function appendMessage(role, content, memories = [], usageInfo = null, usedAgent = null, sources = []) {
  removeEmptyState();

  const messagesEl = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = `message-row ${role}`;

  const avatar = document.createElement('div');
  avatar.className = `avatar ${role}`;
  avatar.textContent = role === 'user' ? '나' : 'G';

  const wrap = document.createElement('div');
  wrap.className = 'message-content-wrap';

  if (role === 'assistant' && usageInfo?.thinking) {
    const thinkBlock = document.createElement('div');
    thinkBlock.className = 'thinking-block';

    const toggle = document.createElement('button');
    toggle.className = 'thinking-toggle';
    toggle.innerHTML = '🧠 추론 과정 보기 <span class="th-caret">▾</span>';

    const contentEl = document.createElement('div');
    contentEl.className = 'thinking-content';
    contentEl.textContent = usageInfo.thinking;

    toggle.onclick = () => {
      const open = contentEl.classList.toggle('open');
      toggle.classList.toggle('open', open);
      toggle.innerHTML = (open ? '🧠 추론 과정 숨기기' : '🧠 추론 과정 보기') +
        ' <span class="th-caret">▾</span>';
    };

    thinkBlock.appendChild(toggle);
    thinkBlock.appendChild(contentEl);
    wrap.appendChild(thinkBlock);
  }

  const bubble = document.createElement('div');
  if (role === 'assistant') {
    bubble.className = 'bubble md';
    bubble.innerHTML = renderMarkdown(content);
    highlightCodeBlocks(bubble);
  } else {
    bubble.className = 'bubble';
    bubble.textContent = content;
  }
  wrap.appendChild(bubble);

  if (role === 'assistant' && memories.length > 0) {
    const memRefs = document.createElement('div');
    memRefs.className = 'memory-refs';

    const toggle = document.createElement('button');
    toggle.className = 'memory-toggle';
    toggle.innerHTML = `💡 메모리 ${memories.length}건 참조 ▾`;

    const list = document.createElement('div');
    list.className = 'memory-list';
    memories.forEach(m => {
      const item = document.createElement('div');
      item.className = 'memory-item';
      item.innerHTML = `<span class="memory-type">${esc(m.type)}</span>${esc(m.content)}`;
      list.appendChild(item);
    });

    toggle.onclick = () => {
      const open = list.classList.toggle('open');
      toggle.innerHTML = `💡 메모리 ${memories.length}건 참조 ${open ? '▴' : '▾'}`;
    };

    memRefs.appendChild(toggle);
    memRefs.appendChild(list);
    wrap.appendChild(memRefs);
  }

  if (role === 'assistant' && Array.isArray(sources) && sources.length > 0) {
    // 스트림(stream.js) 과 동일하게 wrap 최상단에 배치한다.
    wrap.insertBefore(renderSourceRefs(null, sources), wrap.firstChild);
  }

  row.appendChild(avatar);
  row.appendChild(wrap);
  messagesEl.appendChild(row);

  if (role === 'assistant' && usedAgent) {
    const badge = document.createElement('div');
    badge.className = 'agent-used-badge';
    badge.textContent = `${usedAgent.icon} ${usedAgent.name}`;
    wrap.appendChild(badge);
  }

  if (role === 'assistant' && usageInfo?.context_pct != null) {
    updateContextBar(usageInfo, null);
  }

  return row;
}

export function appendLoadingBubble() {
  removeEmptyState();
  const messagesEl = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = 'message-row assistant';
  row.id = 'loading-bubble';
  row.innerHTML = `
    <div class="avatar assistant">G</div>
    <div class="message-content-wrap">
      <div class="bubble loading-bubble">
        <div class="dot"></div><div class="dot"></div><div class="dot"></div>
      </div>
    </div>`;
  messagesEl.appendChild(row);
  scrollToBottom();
}

export function removeLoadingBubble() {
  document.getElementById('loading-bubble')?.remove();
}
