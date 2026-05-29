// frontend/js/sim/export/markdown.js
// Markdown export for scenario feed and agent context window.

import { sim, agentLabel } from '../state.js';
import { stripCodeFence } from '../utils/json.js';

const EMOTION_EMOJI = { happy: '😊', angry: '😤', sad: '😢', fear: '😨', neutral: '😐' };

function downloadMd(content, filename) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function safeFilename(str) {
  return str.replace(/[/\\:*?"<>|]/g, '_').slice(0, 80);
}

function nowTag() {
  const d = new Date();
  return d.toISOString().slice(0, 16).replace('T', '_').replace(':', '');
}

function fmtKo(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('ko-KR', {
    year: 'numeric', month: 'long', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function metaLine(meta) {
  if (!meta) return '';
  const emotion = meta.emotion;
  const action  = meta.action;
  const parts   = [];
  if (emotion) parts.push(`${EMOTION_EMOJI[emotion] || '😐'} ${emotion}`);
  if (action && action !== 'speak') parts.push(`· ${action}`);
  return parts.join(' ');
}

// ── Scenario feed export ──────────────────────────────────────────────────────

export async function exportScenarioMarkdown() {
  const [logRes, statusRes] = await Promise.all([
    fetch('/api/simulation/logs'),
    fetch('/api/simulation/status'),
  ]);
  const log    = logRes.ok    ? await logRes.json()    : [];
  const status = statusRes.ok ? await statusRes.json() : {};

  const scenarioName = sim.currentScenarioName || '시나리오';
  const nowKo        = new Date().toLocaleString('ko-KR', {
    year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const filename     = safeFilename(`${scenarioName}_${nowTag()}`) + '.md';

  const startTs     = log[0]?.timestamp;
  const endTs       = log.length > 1 ? log[log.length - 1]?.timestamp : null;
  const maxWave     = log.length ? Math.max(...log.map(e => e.wave ?? 0)) : 0;
  const statusLabel = { done: '완료 ✅', stopped: '중지 ⏹', running: '실행 중 ▶', error: '오류 ❌' }[status.status] ?? (status.status || '—');

  let md = '';

  // Title + meta block
  md += `# ${scenarioName}\n\n`;
  md += `> **추출 일시** ${nowKo}\n`;
  if (startTs) md += `> **시작** ${fmtKo(startTs)}\n`;
  if (endTs)   md += `> **종료** ${fmtKo(endTs)}\n`;
  if (maxWave > 0) md += `> **Wave** ${maxWave} · **총 턴** ${log.length}\n`;
  md += `> **상태** ${statusLabel}\n`;
  md += `\n---\n\n`;

  // Agents table
  md += `## 등장인물\n\n`;
  md += `| 아이콘 | 이름 | ID | 그룹 | 초기 활성 |\n`;
  md += `|--------|------|----|------|-----------|\n`;
  for (const a of sim.agents) {
    const groups = (a.groups || []).join(', ') || '—';
    const active = a.initial_active !== false ? '✅' : '—';
    md += `| ${a.icon || '🤖'} | ${a.display_name || a.name} | \`${a.name}\` | ${groups} | ${active} |\n`;
  }
  md += `\n`;

  // Background
  if (sim.background) {
    md += `## 배경\n\n${sim.background}\n\n---\n\n`;
  }

  // Conversation log
  md += `## 대화 기록\n\n`;
  if (!log.length) {
    md += `*대화 기록이 없습니다.*\n\n`;
  } else {
    let curWave = null;
    for (const entry of log) {
      const wave = entry.wave ?? null;
      if (wave !== curWave) {
        curWave = wave;
        md += wave != null ? `### 🌊 Wave ${wave}\n\n` : `### —\n\n`;
      }

      const agent     = sim.agents.find(a => a.name === entry.speaker);
      const icon      = agent?.icon || '🤖';
      const name      = agent?.display_name || entry.speaker;
      const targets   = (entry.targets || []).filter(t => t !== 'system');
      const targetStr = targets.length
        ? `→ *${targets.map(t => t === 'all' ? '전체' : agentLabel(t)).join(', ')}*`
        : '*(독백)*';
      const meta = metaLine(entry.meta);

      md += `---\n\n`;
      md += `**${icon} ${name}** \`${entry.speaker}\` ${targetStr}\n`;
      if (meta) md += `${meta}\n`;
      md += `\n`;
      md += `> ${entry.content.replace(/\n/g, '\n> ')}\n\n`;
      if (entry.action_note) md += `*(${entry.action_note})*\n\n`;
    }
  }

  downloadMd(md, filename);
}

// ── Agent context window export ───────────────────────────────────────────────

export async function exportAgentContextMarkdown(agentName) {
  const res = await fetch(`/api/simulation/agents/${encodeURIComponent(agentName)}/context`);
  if (!res.ok) { alert('컨텍스트 불러오기 실패'); return; }
  const data = await res.json();

  const agent       = sim.agents.find(a => a.name === agentName);
  const icon        = agent?.icon || '🤖';
  const displayName = agent?.display_name || agentName;
  const groups      = (agent?.groups || []).join(', ') || '—';
  const nowKo       = new Date().toLocaleString('ko-KR', {
    year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const filename    = safeFilename(`${displayName}_컨텍스트_${nowTag()}`) + '.md';

  const { messages = [], prompt_tokens = 0, token_limit = 0, trimmed = 0, memory_size = 0 } = data;
  const pct = token_limit > 0 ? ((prompt_tokens / token_limit) * 100).toFixed(1) : '—';

  let md = '';

  // Title + meta block
  md += `# ${icon} ${displayName} — 컨텍스트 윈도우\n\n`;
  md += `> **에이전트** \`${agentName}\` · **그룹** ${groups}\n`;
  md += `> **메모리** ${memory_size}개 메시지 · **토큰** ${prompt_tokens.toLocaleString()} / ${token_limit.toLocaleString()} (${pct}%)\n`;
  if (trimmed > 0) md += `> ⚠ **트림** ${trimmed}개 메시지 제거됨\n`;
  md += `> **추출 시각** ${nowKo}\n`;
  md += `\n---\n\n`;

  // Messages
  for (const msg of messages) {
    if (msg.role === 'system') {
      const splitIdx = msg.content.indexOf('\n[Important Output Format]');
      const userPrompt  = splitIdx >= 0 ? msg.content.slice(0, splitIdx).trim() : msg.content.trim();
      const outputFmt   = splitIdx >= 0 ? msg.content.slice(splitIdx).trim()    : '';

      md += `## 시스템 프롬프트\n\n`;
      md += userPrompt + '\n\n';
      if (outputFmt) {
        md += `<details>\n<summary>Output Format 지시문 (펼치기)</summary>\n\n`;
        md += '```\n' + outputFmt + '\n```\n\n';
        md += `</details>\n\n`;
      }
      md += `---\n\n`;
      continue;
    }

    if (msg.role === 'user') {
      const bgMatch  = msg.content.match(/^\[배경\]\s*([\s\S]*)$/);
      const spkMatch = msg.content.match(/^\[([^\]]+)\]\s*([\s\S]*)$/);
      if (bgMatch) {
        md += `### [배경]\n\n${bgMatch[1].trim()}\n\n---\n\n`;
      } else if (spkMatch) {
        const actionMatch  = spkMatch[2].match(/^([\s\S]*)\n\(([^)]*)\)\s*$/);
        const inContent    = actionMatch ? actionMatch[1] : spkMatch[2];
        const inActionNote = actionMatch ? actionMatch[2] : '';
        md += `### 📨 [${agentLabel(spkMatch[1])}] → 나\n\n`;
        md += `> ${inContent.trim().replace(/\n/g, '\n> ')}\n\n`;
        if (inActionNote) md += `*(${inActionNote})*\n\n`;
      } else {
        md += `### 📩 user\n\n> ${msg.content.trim().replace(/\n/g, '\n> ')}\n\n`;
      }
      continue;
    }

    if (msg.role === 'assistant') {
      let parsed = null;
      try { parsed = JSON.parse(stripCodeFence(msg.content).trim()); } catch (_) {}

      if (parsed) {
        const rawTargets = Array.isArray(parsed.target)
          ? parsed.target
          : (parsed.target ? [parsed.target] : []);
        const tgt = rawTargets
          .map(t => t === 'all' ? '전체' : t === 'system' ? '(독백)' : agentLabel(t))
          .join(', ') || '(독백)';
        const meta       = metaLine(parsed);
        const actionNote = parsed.action_note || '';

        md += `### 💬 내 발언 → ${tgt}\n\n`;
        if (meta) md += `${meta}\n\n`;
        md += `> ${(parsed.content || '').replace(/\n/g, '\n> ')}\n\n`;
        if (actionNote) md += `*(${actionNote})*\n\n`;
      } else {
        md += `### 💬 내 발언\n\n\`\`\`json\n${msg.content}\n\`\`\`\n\n`;
      }
    }
  }

  downloadMd(md, filename);
}
