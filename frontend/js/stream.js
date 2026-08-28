import { scrollToBottom, removeEmptyState } from './utils.js';
import { renderMarkdown, highlightCodeBlocks } from './markdown.js';
import { updateContextBar } from './context-bar.js';
import { esc } from './utils.js';
import { renderSourceRefs } from './sources.js';

export function createStreamRow() {
  removeEmptyState();
  const messagesEl = document.getElementById('messages');
  const row = document.createElement('div');
  row.className = 'message-row assistant';
  const avatar = document.createElement('div');
  avatar.className = 'avatar assistant';
  avatar.textContent = 'G';
  const wrap = document.createElement('div');
  wrap.className = 'message-content-wrap';
  row.appendChild(avatar);
  row.appendChild(wrap);
  messagesEl.appendChild(row);
  scrollToBottom();
  return row;
}

export async function readSSEStream(body, row) {
  const wrap          = row.querySelector('.message-content-wrap');
  let thinkingBlock   = null;
  let thinkingToggle  = null;
  let thinkingContent = null;
  let bubble          = null;
  let answerBuf       = '';
  let renderTimer     = null;
  let isFirstAnswer   = true;
  let searchRendered  = false;

  function ensureThinking() {
    if (thinkingBlock) return;
    thinkingBlock = document.createElement('div');
    thinkingBlock.className = 'thinking-block';

    thinkingToggle = document.createElement('button');
    thinkingToggle.className = 'thinking-toggle open streaming';
    thinkingToggle.innerHTML = '🧠 사고 중... <span class="th-caret">▾</span>';
    thinkingToggle.disabled = true;

    thinkingContent = document.createElement('div');
    thinkingContent.className = 'thinking-content streaming';

    thinkingBlock.appendChild(thinkingToggle);
    thinkingBlock.appendChild(thinkingContent);
    wrap.appendChild(thinkingBlock);
  }

  function ensureBubble() {
    if (bubble) return;
    bubble = document.createElement('div');
    bubble.className = 'bubble md streaming-cursor';
    wrap.appendChild(bubble);
  }

  function collapseThinking() {
    if (!thinkingBlock || !isFirstAnswer) return;
    isFirstAnswer = false;
    const len = thinkingContent.textContent.length;
    thinkingContent.classList.remove('streaming', 'open');
    thinkingToggle.classList.remove('streaming', 'open');
    thinkingToggle.disabled = false;
    thinkingToggle.innerHTML = `🧠 추론 과정 (${len.toLocaleString()}자) <span class="th-caret">▾</span>`;
    thinkingToggle.onclick = () => {
      const open = thinkingContent.classList.toggle('open');
      thinkingToggle.classList.toggle('open', open);
    };
  }

  function scheduleRender() {
    if (renderTimer) return;
    renderTimer = setTimeout(() => {
      renderTimer = null;
      if (bubble && answerBuf) {
        bubble.innerHTML = renderMarkdown(answerBuf);
        highlightCodeBlocks(bubble);
        bubble.classList.add('streaming-cursor');
      }
    }, 80);
  }

  function handleEvent(type, data) {
    if (type === 'search') {
      searchRendered = true;
      const box = renderSourceRefs(data.query, data.results);
      wrap.insertBefore(box, wrap.firstChild);
      scrollToBottom();

    } else if (type === 'thinking') {
      ensureThinking();
      thinkingContent.textContent += data.chunk;
      thinkingContent.scrollTop = thinkingContent.scrollHeight;
      scrollToBottom();

    } else if (type === 'answer') {
      collapseThinking();
      ensureBubble();
      answerBuf += data.chunk;
      scheduleRender();
      scrollToBottom();

    } else if (type === 'done') {
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      if (bubble && answerBuf) {
        bubble.innerHTML = renderMarkdown(answerBuf);
        highlightCodeBlocks(bubble);
        bubble.classList.remove('streaming-cursor');
      }
      collapseThinking();

      // search 이벤트로 이미 렌더한 경우 done.sources 는 무시 (중복 방지).
      if (!searchRendered && data.sources?.length > 0) {
        const box = renderSourceRefs(null, data.sources);
        wrap.insertBefore(box, wrap.firstChild);
      }

      if (data.memories?.length > 0) {
        const memRefs = document.createElement('div');
        memRefs.className = 'memory-refs';
        const toggle = document.createElement('button');
        toggle.className = 'memory-toggle';
        toggle.innerHTML = `💡 메모리 ${data.memories.length}건 참조 ▾`;
        const list = document.createElement('div');
        list.className = 'memory-list';
        data.memories.forEach(m => {
          const item = document.createElement('div');
          item.className = 'memory-item';
          item.innerHTML = `<span class="memory-type">${esc(m.type)}</span>${esc(m.content)}`;
          list.appendChild(item);
        });
        toggle.onclick = () => {
          const open = list.classList.toggle('open');
          toggle.innerHTML = `💡 메모리 ${data.memories.length}건 참조 ${open ? '▴' : '▾'}`;
        };
        memRefs.appendChild(toggle);
        memRefs.appendChild(list);
        wrap.appendChild(memRefs);
      }

      if (data.used_agent) {
        const badge = document.createElement('div');
        badge.className = 'agent-used-badge';
        badge.textContent = `${data.used_agent.icon} ${data.used_agent.name}`;
        wrap.appendChild(badge);
      }

      if (data.usage) updateContextBar(data.usage, data.used_server);
      if (data.title) document.getElementById('chat-title').textContent = data.title;

      if (data.archived_count > 0) {
        const notice = document.createElement('div');
        notice.className = 'archive-notice';
        notice.textContent = `${data.archived_count}개 메시지가 메모리로 저장됨`;
        document.getElementById('messages').appendChild(notice);
      }
      scrollToBottom();

    } else if (type === 'error') {
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      ensureBubble();
      bubble.classList.remove('streaming-cursor');
      bubble.textContent = `오류가 발생했습니다: ${data.message}`;
    }
  }

  const reader  = body.getReader();
  const decoder = new TextDecoder();
  let sseBuf = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      sseBuf += decoder.decode(value, { stream: true });
      const parts = sseBuf.split('\n\n');
      sseBuf = parts.pop();

      for (const part of parts) {
        if (!part.trim()) continue;
        let evType = '', evData = '';
        for (const line of part.split('\n')) {
          if (line.startsWith('event: '))     evType = line.slice(7).trim();
          else if (line.startsWith('data: ')) evData = line.slice(6).trim();
        }
        if (evType && evData) {
          try { handleEvent(evType, JSON.parse(evData)); }
          catch (e) { console.error('SSE parse error', e, evData); }
        }
      }
    }
  } finally {
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    reader.releaseLock();
  }
}
