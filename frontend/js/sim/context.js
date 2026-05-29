// frontend/js/sim/context.js
// Agent context-window viewer (right panel "context" tab).

import { sim, esc, emotionClass, agentLabel } from './state.js';
import { stripCodeFence } from './utils/json.js';
import { exportAgentContextMarkdown } from './export/markdown.js';

export function switchTab(tabName) {
  document.querySelectorAll('.sim-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.getElementById('sim-tab-graph').classList.toggle('sim-hidden', tabName !== 'graph');
  document.getElementById('sim-tab-context').classList.toggle('sim-hidden', tabName !== 'context');
  document.getElementById('sim-export-graph-btn').classList.toggle('sim-hidden', tabName !== 'graph');
  if (tabName === 'context' && sim.selectedAgent && sim.status !== 'idle') {
    fetchAgentContext(sim.selectedAgent);
  }
}

export async function openAgentContext(name) {
  sim.selectedAgent = name;
  switchTab('context');
  const a = sim.agents.find(ag => ag.name === name);
  const label = a
    ? `${a.icon} ${a.display_name || a.name}${a.display_name ? ` (${a.name})` : ''}`
    : `🤖 ${name}`;
  document.getElementById('sim-context-agent-name').textContent = label;
  document.getElementById('sim-context-refresh-btn').classList.remove('sim-hidden');

  const ctxMdBtn = document.getElementById('sim-export-ctx-md-btn');
  if (ctxMdBtn) {
    ctxMdBtn.classList.remove('sim-hidden');
    // 클릭마다 올바른 에이전트를 내보내도록 리스너 교체
    const newBtn = ctxMdBtn.cloneNode(true);
    ctxMdBtn.replaceWith(newBtn);
    newBtn.addEventListener('click', () => exportAgentContextMarkdown(name));
  }

  await fetchAgentContext(name);
}

export async function fetchAgentContext(name) {
  const msgs = document.getElementById('sim-context-msgs');
  msgs.innerHTML = '<div style="padding:12px;font-size:11px;color:#94a3b8;">로딩 중...</div>';
  try {
    const res = await fetch(`/api/simulation/agents/${encodeURIComponent(name)}/context`);
    if (!res.ok) {
      msgs.innerHTML = `<div style="padding:12px;font-size:11px;color:#ef4444;">불러오기 실패 (${res.status})<br>시뮬레이션이 실행된 후 확인 가능합니다.</div>`;
      return;
    }
    const data = await res.json();
    renderContextMessages(data.messages, data.trimmed || 0, data.prompt_tokens || 0, data.token_limit || 0);
  } catch (e) {
    msgs.innerHTML = `<div style="padding:12px;font-size:11px;color:#ef4444;">오류: ${esc(String(e))}</div>`;
  }
}

function renderContextMessages(messages, trimmed = 0, promptTokens = 0, tokenLimit = 0) {
  const container = document.getElementById('sim-context-msgs');
  container.innerHTML = '';

  // Token usage bar
  if (tokenLimit > 0) {
    const pct = Math.min(100, (promptTokens / tokenLimit) * 100);
    const bar = document.createElement('div');
    bar.className = 'ctx-token-banner';
    bar.innerHTML = `
      <div class="ctx-token-info">
        <span class="ctx-token-used">${promptTokens.toLocaleString()}</span>
        <span class="ctx-token-sep">/</span>
        <span class="ctx-token-limit">${tokenLimit.toLocaleString()} 토큰</span>
        <span class="ctx-token-pct ${pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : ''}">(${pct.toFixed(1)}%)</span>
        ${trimmed > 0 ? `<span class="ctx-trim-badge">⚠ ${trimmed}개 제거됨</span>` : ''}
      </div>
      <div class="ctx-token-bar-wrap">
        <div class="ctx-token-bar-fill ${pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : ''}"
             style="width:${pct}%"></div>
      </div>
    `;
    container.appendChild(bar);
  } else if (trimmed > 0) {
    const warn = document.createElement('div');
    warn.className = 'ctx-trim-warning';
    warn.textContent = `⚠ 이전 ${trimmed}개 메시지가 토큰 한도 초과로 제거되었습니다`;
    container.appendChild(warn);
  }

  messages.forEach(msg => {
    const div = document.createElement('div');
    div.className = 'ctx-msg';

    if (msg.role === 'system') {
      div.innerHTML = `
        <div class="ctx-role ctx-role-system">system</div>
        <details class="ctx-system-details">
          <summary>시스템 프롬프트 (클릭하여 펼치기)</summary>
          <pre class="ctx-content">${esc(msg.content)}</pre>
        </details>`;

    } else if (msg.role === 'user') {
      const bgMatch  = msg.content.match(/^\[배경\]\s*([\s\S]*)$/);
      const spkMatch = msg.content.match(/^\[([^\]]+)\]\s*([\s\S]*)$/);

      if (bgMatch) {
        div.innerHTML = `
          <div class="ctx-role ctx-role-background">배경</div>
          <div class="ctx-content">${esc(bgMatch[1])}</div>`;
      } else if (spkMatch) {
        const inActionMatch = spkMatch[2].match(/^([\s\S]*)\n\(([^)]*)\)\s*$/);
        const inContent     = inActionMatch ? inActionMatch[1] : spkMatch[2];
        const inActionNote  = inActionMatch ? inActionMatch[2] : '';
        div.innerHTML = `
          <div class="ctx-role ctx-role-incoming">
            <span class="ctx-speaker-badge">${esc(agentLabel(spkMatch[1]))}</span>incoming
          </div>
          <div class="ctx-content">${esc(inContent)}</div>
          ${inActionNote ? `<div class="sim-feed-action">*${esc(inActionNote)}*</div>` : ''}`;
      } else {
        div.innerHTML = `
          <div class="ctx-role ctx-role-incoming">user</div>
          <div class="ctx-content">${esc(msg.content)}</div>`;
      }

    } else if (msg.role === 'assistant') {
      let parsed = null;
      try {
        const raw = stripCodeFence(msg.content);
        parsed = JSON.parse(raw.trim());
      } catch (_) {}

      if (parsed) {
        const rawTargets = Array.isArray(parsed.target) ? parsed.target : (parsed.target ? [parsed.target] : []);
        const tgt = rawTargets.map(t => (t === 'all' ? '전체' : t === 'system' ? '(독백)' : agentLabel(t))).join(', ');
        const ctxActionNote = parsed.action_note || '';
        const metaBadges = sim.extra_fields
          .filter(f => f.name !== 'action_note' && parsed[f.name] != null)
          .map(f => {
            const val = String(parsed[f.name]);
            const cls = f.name === 'emotion' ? emotionClass(val) : 'emotion-neutral';
            return `<span class="sim-feed-badge ${cls}">${esc(val)}</span>`;
          }).join('');
        div.innerHTML = `
          <div class="ctx-role ctx-role-assistant">assistant</div>
          <div class="ctx-parsed-content">"${esc(parsed.content ?? '')}"</div>
          ${ctxActionNote ? `<div class="sim-feed-action">*${esc(ctxActionNote)}*</div>` : ''}
          <div class="ctx-parsed-meta">
            ${metaBadges}
            <span class="ctx-targets">→ ${esc(tgt || 'system')}</span>
          </div>`;
      } else {
        div.innerHTML = `
          <div class="ctx-role ctx-role-assistant">assistant</div>
          <div class="ctx-content">${esc(msg.content)}</div>`;
      }
    }

    container.appendChild(div);
  });

  container.lastElementChild?.scrollIntoView({ block: 'end' });
}
