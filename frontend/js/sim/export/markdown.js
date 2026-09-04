// frontend/js/sim/export/markdown.js
// Markdown export: screenplay-style with selectable event types.
//
// 다른 언어 구현 위치: ABM/export/markdown.py (+ 헬퍼는 ABM/export/labels.py).
// 브라우저 다운로드 버튼은 이 파일을, `python -m ABM.cli` 는 파이썬 쪽을 쓴다.
// 두 출력은 글자 단위로 같아야 하며 tests/fixtures/*.md 골든 테스트가 파이썬 쪽을
// 고정한다 — 포맷(헤더·등장인물표·wave 헤딩·fmt* 문구)을 바꿀 때는 반드시 양쪽을
// 함께 고칠 것. 아래 _buildMarkdown / buildStream / fmt* 가 1:1 대응 지점이다.

import { sim, agentLabel, getAgentIcon, simTimeLabel, normalizeWeekday,
         normalizeTargetDuration, buildInfectionModel, infectionBadge,
         meetingNarration, formatDayHour } from '../state.js';
import { stripCodeFence } from '../utils/json.js';
import { downloadFile, safeFilename, nowTag } from '../utils/download.js';
import { exportLocationHistoryCsv } from './csv.js';

const EMOTION_EMOJI = { happy: '😊', angry: '😤', sad: '😢', fear: '😨', neutral: '😐' };

// ── Utilities ─────────────────────────────────────────────────────────────────

function downloadMd(content, filename) {
  downloadFile(content, filename, 'text/markdown;charset=utf-8');
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
  const parts = [];
  if (meta.emotion) parts.push(`${EMOTION_EMOJI[meta.emotion] || '😐'} ${meta.emotion}`);
  if (meta.action && meta.action !== 'speak') parts.push(`· ${meta.action}`);
  return parts.join(' ');
}

// ── Export modal ──────────────────────────────────────────────────────────────

export function openExportModal() {
  document.getElementById('sim-export-modal')?.classList.remove('sim-hidden');
}

function closeExportModal() {
  document.getElementById('sim-export-modal')?.classList.add('sim-hidden');
}

function readChecks() {
  return {
    time:         document.getElementById('exp-chk-time')?.checked         ?? true,
    action:       document.getElementById('exp-chk-action')?.checked       ?? true,
    move:         document.getElementById('exp-chk-move')?.checked         ?? true,
    appearance:   document.getElementById('exp-chk-appearance')?.checked   ?? true,
    world:        document.getElementById('exp-chk-world')?.checked        ?? true,
    intervention: document.getElementById('exp-chk-intervention')?.checked ?? true,
    infection:    document.getElementById('exp-chk-infection')?.checked    ?? true,
    meeting:      document.getElementById('exp-chk-meeting')?.checked      ?? true,
  };
}

export function initExportModal() {
  document.getElementById('sim-export-modal-close')?.addEventListener('click', closeExportModal);
  document.getElementById('sim-export-modal-cancel')?.addEventListener('click', closeExportModal);
  document.getElementById('sim-export-modal')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) closeExportModal();
  });
  document.getElementById('sim-export-modal-download')?.addEventListener('click', async () => {
    closeExportModal();
    try {
      await exportScenarioMarkdown(readChecks());
    } catch (e) {
      console.error('[export] 마크다운 내보내기 실패:', e);
      alert(`마크다운 내보내기 실패: ${e.message}`);
    }
  });
  // 위치 이력 CSV — 체크박스 선택과 무관한 별도 산출물이라 같은 모달에서 바로 내려받는다.
  document.getElementById('sim-export-modal-csv')?.addEventListener('click', async () => {
    closeExportModal();
    try {
      await exportLocationHistoryCsv();
    } catch (e) {
      console.error('[export] 위치 이력 CSV 내보내기 실패:', e);
      alert(`위치 이력 CSV 내보내기 실패: ${e.message}`);
    }
  });
}

// ── Data fetching ─────────────────────────────────────────────────────────────

async function fetchAll() {
  const [logRes, statusRes, evtRes] = await Promise.all([
    fetch('/api/simulation/logs'),
    fetch('/api/simulation/status'),
    fetch('/api/simulation/events'),
  ]);
  return {
    log:    logRes.ok    ? await logRes.json()    : [],
    status: statusRes.ok ? await statusRes.json() : {},
    events: evtRes.ok    ? await evtRes.json()    : [],
  };
}

// ── Merge logs + events into a single wave-ordered stream ─────────────────────

function buildStream(log, events, checks) {
  // Collect active event types based on checkboxes
  const wantTypes = new Set();
  if (checks.move)         wantTypes.add('agent_move');
  if (checks.appearance)   wantTypes.add('appearance_update');
  if (checks.intervention) wantTypes.add('system_intervention');
  if (checks.world)        wantTypes.add('world_event');
  if (checks.infection)    wantTypes.add('infection_update');
  if (checks.meeting)      wantTypes.add('meeting_update');

  // Normalise events: { wave, sort_key, kind, payload }
  const items = [];

  for (const entry of log) {
    items.push({ wave: entry.wave ?? 0, ts: entry.timestamp ?? 0, kind: 'dialogue', payload: entry });
  }
  for (const evt of events) {
    if (!wantTypes.has(evt.event_type)) continue;
    items.push({ wave: evt.wave ?? 0, ts: evt.timestamp ?? 0, kind: evt.event_type, payload: evt.data });
  }

  // Sort by (wave, timestamp) so events interleave naturally with dialogue
  items.sort((a, b) => a.wave !== b.wave ? a.wave - b.wave : a.ts - b.ts);
  return items;
}

// ── Markdown formatters per event kind ───────────────────────────────────────

function fmtDialogue(entry, checks) {
  const agent     = sim.agents.find(a => a.name === entry.speaker);
  const icon      = getAgentIcon(agent || { name: entry.speaker }, (entry.meta || {}).emotion);
  const name      = agent?.display_name || entry.speaker;
  const targets   = (entry.targets || []).filter(t => t !== 'self' && t !== 'system');
  const targetStr = targets.length
    ? `→ *${targets.map(t => t === 'all' ? '전체' : agentLabel(t)).join(', ')}*`
    : '*(독백)*';
  const meta = metaLine(entry.meta);

  let s = `\n**${icon} ${name}** ${targetStr}`;
  if (meta) s += `  \`${meta}\``;
  s += '\n';
  s += `> ${(entry.content ?? '').replace(/\n/g, '\n> ')}\n`;
  if (checks.action && entry.action_note) s += `> *(${entry.action_note})*\n`;
  return s;
}

function fmtMove(data) {
  const name = data.display_name || data.agent;
  if (data.to_exterior) return `\n> **[씬]** *${name}이(가) 자리를 떴다. (→ ${data.to})*\n`;
  return `\n> **[씬]** *${name}이(가) ${data.from ? `${data.from}에서 ` : ''}${data.to}(으)로 이동했다.*\n`;
}

function fmtAppearance(data) {
  const name = data.display_name || data.agent;
  return `\n> **[씬]** *${name}의 외모가 변했다: ${data.description}*\n`;
}

function fmtIntervention(data) {
  const icon = data.icon || '🎬';
  const nm   = data.display_name || '내레이터';
  const tgt  = data.target_alias || data.target || '';
  return `\n> **[${icon} ${nm}]** → *${tgt}* : ${data.message}\n`;
}

function fmtWorldEvent(data) {
  return `\n> **[🌍 세계 사건]** *${data.content}*\n`;
}

/**
 * 감염 상태 변화 한 줄. 엔진이 판정한 사실이지 등장인물이 아는 정보가 아니므로
 * 다른 씬 이벤트와 같은 형식으로 관전자 시점 서술처럼 적는다.
 */
function fmtInfection(data) {
  const badge = infectionBadge(data.status, data.cause);
  if (!badge) return '';
  const name    = data.display_name || agentLabel(data.agent);
  // 이벤트 페이로드가 우선. 구버전/누락 시 실행 설정의 질병명으로 폴백한다.
  const disease = data.disease_name || sim.infection_model?.disease_name || '';
  const what    = disease ? `${disease}에` : '병에';
  const text = data.cause === 'recovery'
    ? `${name}이(가) ${disease ? `${disease}에서 ` : ''}회복했다.${data.status === 'R' ? ' (면역)' : ' (재감염 가능)'}`
    : data.cause === 'event'
      ? `${name}이(가) ${what} 감염됐다. (최초 감염자)`
      : `${name}이(가) ${what} 감염됐다. (접촉 전파)`;
  return `\n> **[${badge.icon} 감염]** *${text}*\n`;
}

/**
 * 만남 lock 한 줄. 문구는 피드 카드와 같은 meetingNarration을 쓴다.
 * 모르는 status(구버전/미래 값)면 빈 문자열이라 아무것도 안 실린다.
 */
function fmtMeeting(data) {
  const info = meetingNarration(data);
  if (!info) return '';
  return `\n> **[${info.icon} 씬]** *${info.text}*\n`;
}

// ── Main export ───────────────────────────────────────────────────────────────

function _buildMarkdown(log, events, statusStr, checks) {
  const scenarioName = sim.currentScenarioName || '시나리오';
  const nowKo = new Date().toLocaleString('ko-KR', {
    year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });

  const startTs     = log[0]?.timestamp;
  const endTs       = log.length > 1 ? log[log.length - 1]?.timestamp : null;
  const maxWave     = log.length ? Math.max(...log.map(e => e.wave ?? 0)) : 0;
  const statusLabel = { done: '완료 ✅', stopped: '중지 ⏹', running: '실행 중 ▶', error: '오류 ❌' }[statusStr] ?? (statusStr || '—');

  let md = '';

  md += `# ${scenarioName}\n\n`;
  md += `> **추출 일시** ${nowKo}\n`;
  if (startTs) md += `> **시작** ${fmtKo(startTs)}\n`;
  if (endTs)   md += `> **종료** ${fmtKo(endTs)}\n`;
  if (maxWave > 0) md += `> **Wave** ${maxWave} · **총 턴** ${log.length}\n`;
  md += `> **상태** ${statusLabel}\n`;
  md += `\n---\n\n`;

  md += `## 등장인물\n\n`;
  md += `| 아이콘 | 이름 | ID | 그룹 | 초기 활성 |\n`;
  md += `|--------|------|----|------|-----------|\n`;
  for (const a of sim.agents) {
    const groups = (a.groups || []).join(', ') || '—';
    const active = a.initial_active !== false ? '✅' : '—';
    md += `| ${a.icon || '🤖'} | ${a.display_name || a.name} | \`${a.name}\` | ${groups} | ${active} |\n`;
  }
  md += `\n`;

  if (sim.background) {
    md += `## 배경\n\n${sim.background}\n\n---\n\n`;
  }

  // 감염병 모델이 켜져 있을 때만 — 꺼진 실행에는 아무 영향이 없는 설정이라 노이즈다.
  const infection = buildInfectionModel(sim.infection_model);
  if (infection.enabled) {
    md += `## 🦠 감염병 모델\n\n`;
    md += `> **질병** ${infection.disease_name || '(이름 없음)'}\n`;
    const recovery = infection.recovery_max_minutes === 0
      ? '자연 회복 없음(만성)'
      : `${formatDayHour(infection.recovery_min_minutes)} ~ ${formatDayHour(infection.recovery_max_minutes)}`;
    md += `> **전염 확률** ${infection.transmission_probability} · **회복까지** ${recovery}\n`;
    md += `> **회복 후** ${infection.immune_after_recovery ? '면역 획득 (SIR)' : '재감염 가능 (SIS)'}\n\n`;
    if (infection.symptom_stages.length) {
      md += `| 단계 | 감염 후 경과 시간 | 증상 서사 |\n|------|------------------|-----------|\n`;
      for (const s of infection.symptom_stages) {
        const text = (s.symptom_text || '').replace(/\|/g, '\\|').replace(/\n/g, ' ');
        md += `| ${s.label} | ${formatDayHour(s.min_minutes)} ~ ${formatDayHour(s.max_minutes)} | ${text} |\n`;
      }
      md += `\n`;
    }
    md += `---\n\n`;
  }

  md += `## 대화 기록\n\n`;
  if (!log.length) {
    md += `*대화 기록이 없습니다.*\n\n`;
  } else {
    // wave의 첫 병합 항목이 time_str이 없는 이벤트(agent_move 등)일 수 있으므로,
    // dialogue 로그 전체에서 wave별 time_str을 먼저 찾아둔다 (첫 항목만 보면 놓칠 수 있음).
    const waveTimeMap = {};
    for (const entry of log) {
      if (entry.wave != null && waveTimeMap[entry.wave] == null && entry.time_str) {
        waveTimeMap[entry.wave] = entry.time_str;
      }
    }
    const stream = buildStream(log, events, checks);
    let curWave  = null;
    for (const item of stream) {
      if (item.wave !== curWave) {
        curWave = item.wave;
        // 서버가 기록해둔 time_str(정확값)을 우선 사용하고, 없으면(구버전 로그 등) fixed 공식으로 폴백.
        const timeLabel = checks.time ? (waveTimeMap[curWave] ?? simTimeLabel(curWave)) : null;
        const waveHead  = timeLabel
          ? `### 🕐 ${timeLabel}  ·  Wave ${curWave}`
          : `### 🌊 Wave ${curWave}`;
        md += `\n${waveHead}\n\n---\n`;
      }
      switch (item.kind) {
        case 'dialogue':            md += fmtDialogue(item.payload, checks); break;
        case 'agent_move':          md += fmtMove(item.payload); break;
        case 'appearance_update':   md += fmtAppearance(item.payload); break;
        case 'system_intervention': md += fmtIntervention(item.payload); break;
        case 'world_event':         md += fmtWorldEvent(item.payload); break;
        case 'infection_update':    md += fmtInfection(item.payload); break;
        case 'meeting_update':      md += fmtMeeting(item.payload); break;
      }
    }
    md += '\n';
  }
  return { md, scenarioName };
}

export async function exportScenarioMarkdown(checks) {
  checks = checks ?? readChecks();
  const { log, status, events } = await fetchAll();
  const { md, scenarioName } = _buildMarkdown(log, events, status.status, checks);
  downloadMd(md, safeFilename(`${scenarioName}_${nowTag()}`) + '.md');
}

// 이력 모달에서 직접 내보내기 — DB 데이터를 사용해 in-memory 상태와 무관하게 동작
export async function exportRunMarkdown(runId, run, preloadedLog) {
  let parsedConfig = {};
  try { parsedConfig = JSON.parse(run.config_json || '{}'); } catch (_) {}

  // 필요한 sim.* 필드를 임시로 교체 (formatting 함수들이 sim.*를 직접 참조)
  const prev = {
    agents:              sim.agents,
    background:          sim.background,
    currentScenarioName: sim.currentScenarioName,
    sim_start_time:      sim.sim_start_time,
    sim_start_weekday:   sim.sim_start_weekday,
    time_per_wave:       sim.time_per_wave,
    time_mode:           sim.time_mode,
    target_duration_minutes: sim.target_duration_minutes,
    infection_model:     sim.infection_model,
  };
  sim.agents              = (parsedConfig.agents || []).map(a => ({
    icon: '🤖', groups: [], initial_active: true, relationships: {}, ...a,
  }));
  sim.background          = parsedConfig.background          || '';
  sim.currentScenarioName = run.scenario_name                || '시나리오';
  sim.sim_start_time      = parsedConfig.sim_start_time      || '09:00';
  sim.sim_start_weekday   = normalizeWeekday(parsedConfig.sim_start_weekday);
  sim.time_per_wave       = parsedConfig.time_per_wave       ?? 30;
  sim.time_mode           = parsedConfig.time_mode           || 'fixed';
  // 구버전 run 스냅샷에는 필드가 없다 — null(= 목표 기간 미사용)로 폴백.
  sim.target_duration_minutes = normalizeTargetDuration(parsedConfig.target_duration_minutes);
  // 마찬가지로 없으면 "꺼진 모델"로 폴백 — 이 실행의 감염 설정을 문서 머리에 싣는다.
  sim.infection_model         = buildInfectionModel(parsedConfig.infection_model);

  const defaultChecks = { time: true, action: true, move: true, appearance: true, world: true, intervention: true, infection: true, meeting: true };

  try {
    const evtRes = await fetch(`/api/simulation/runs/${encodeURIComponent(runId)}/events`);
    const events = evtRes.ok ? await evtRes.json() : [];
    const { md, scenarioName } = _buildMarkdown(preloadedLog, events, run.status, defaultChecks);
    downloadMd(md, safeFilename(`${scenarioName}_${nowTag()}`) + '.md');
  } finally {
    Object.assign(sim, prev);
  }
}

// ── Agent context window export (unchanged) ───────────────────────────────────

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
  const filename = safeFilename(`${displayName}_컨텍스트_${nowTag()}`) + '.md';

  const { messages = [], prompt_tokens = 0, token_limit = 0, trimmed = 0, memory_size = 0 } = data;
  const pct = token_limit > 0 ? ((prompt_tokens / token_limit) * 100).toFixed(1) : '—';

  let md = '';
  md += `# ${icon} ${displayName} — 컨텍스트 윈도우\n\n`;
  md += `> **에이전트** \`${agentName}\` · **그룹** ${groups}\n`;
  md += `> **메모리** ${memory_size}개 메시지 · **토큰** ${prompt_tokens.toLocaleString()} / ${token_limit.toLocaleString()} (${pct}%)\n`;
  if (trimmed > 0) md += `> ⚠ **트림** ${trimmed}개 메시지 제거됨\n`;
  md += `> **추출 시각** ${nowKo}\n`;
  md += `\n---\n\n`;

  for (const msg of messages) {
    if (msg.role === 'system') {
      const splitIdx    = msg.content.indexOf('\n[Important Output Format]');
      const userPrompt  = splitIdx >= 0 ? msg.content.slice(0, splitIdx).trim() : msg.content.trim();
      const outputFmt   = splitIdx >= 0 ? msg.content.slice(splitIdx).trim()    : '';
      md += `## 시스템 프롬프트\n\n${userPrompt}\n\n`;
      if (outputFmt) {
        md += `<details>\n<summary>Output Format 지시문 (펼치기)</summary>\n\n\`\`\`\n${outputFmt}\n\`\`\`\n\n</details>\n\n`;
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
          .map(t => t === 'all' ? '전체' : (t === 'self' || t === 'system') ? '(독백)' : agentLabel(t))
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
