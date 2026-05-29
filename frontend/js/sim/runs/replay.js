// frontend/js/sim/runs/replay.js
// Run replay modal: read-only feed + resume / restart actions.

import { sim, esc, emotionClass, agentLabel } from '../state.js';
import { fmtTime, statusIcon } from '../utils/time.js';
import { applyScenario } from '../scenarios.js';
import { renderAgentCards } from '../run/cards.js';
import { renderSettingsPage } from '../settings/page.js';
import { setStatus } from '../run/control.js';
import { connectSSE } from '../run/sse.js';

export async function openRunReplay(runId, runNum) {
  // 기존 모달 제거
  document.getElementById('sim-replay-modal')?.remove();

  const [runRes, logRes] = await Promise.all([
    fetch(`/api/simulation/runs/${encodeURIComponent(runId)}`),
    fetch(`/api/simulation/runs/${encodeURIComponent(runId)}/log`),
  ]);
  if (!runRes.ok || !logRes.ok) { alert('대화 기록을 불러오지 못했습니다.'); return; }

  const run = await runRes.json();
  const log = await logRes.json();

  // config_json 파싱 (빈 객체면 재시작 불가)
  let parsedConfig = null;
  try {
    const c = JSON.parse(run.config_json || '{}');
    if (c.agents && c.agents.length) parsedConfig = c;
  } catch (_) {}

  const modal = document.createElement('div');
  modal.id = 'sim-replay-modal';
  modal.className = 'sim-replay-modal-overlay';
  modal.innerHTML = `
    <div class="sim-replay-modal-box">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span class="sim-replay-run-badge">#${runNum}</span>
          <span>${esc(run.scenario_name || '직접 실행')}</span>
          <span class="sim-replay-status">${statusIcon[run.status] || run.status}</span>
          <span class="sim-replay-meta">${fmtTime(run.started_at, { includeYear: true })} · ${run.total_waves}wave · ${run.total_turns}turn</span>
        </div>
        <div class="sim-replay-actions">
          ${parsedConfig ? `<button id="sim-replay-resume-btn" class="sim-ctrl-btn continue" style="font-size:12px;padding:4px 10px">↩ 이어서</button>` : ''}
          ${parsedConfig ? `<button id="sim-replay-restart-btn" class="sim-ctrl-btn start" style="font-size:12px;padding:4px 10px">▶ 새로 시작</button>` : ''}
          <button id="sim-replay-close-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">✕ 닫기</button>
        </div>
      </div>
      <div class="sim-replay-feed" id="sim-replay-feed">
        ${log.length === 0 ? '<div class="sim-feed-empty-msg">저장된 대화 기록이 없습니다.</div>' : ''}
      </div>
    </div>`;

  document.body.appendChild(modal);

  // 피드 렌더링
  const feedEl = document.getElementById('sim-replay-feed');
  log.forEach(entry => {
    const div = document.createElement('div');
    div.className = 'sim-feed-item';
    const agentObj = sim.agents.find(a => a.name === entry.speaker);
    const icon = agentObj?.icon || '🤖';
    const label = agentObj?.display_name || entry.speaker;
    const actionNote = entry.action_note || '';
    const extraFields = sim.extra_fields || [];
    const metaBadges = extraFields
      .filter(f => f.name !== 'action_note' && entry.meta && entry.meta[f.name] != null)
      .map(f => {
        const val = String(entry.meta[f.name]);
        const cls = f.name === 'emotion' ? emotionClass(val) : 'emotion-neutral';
        return `<span class="sim-feed-badge ${cls}">${esc(val)}</span>`;
      }).join('');
    const targetArr = Array.isArray(entry.targets) ? entry.targets : [];
    const targets = targetArr.map(t => t === 'all' ? '전체' : agentLabel(t)).join(', ');
    div.innerHTML = `
      <div class="sim-feed-speaker">
        <span class="sim-feed-icon">${esc(icon)}</span>
        <span class="sim-feed-name">${esc(label)}</span>
        <span class="sim-feed-wave-badge">W${entry.wave}</span>
      </div>
      <div class="sim-feed-body">
        <div class="sim-feed-content">${esc(entry.content)}</div>
        ${actionNote ? `<div class="sim-feed-action">*${esc(actionNote)}*</div>` : ''}
        <div class="sim-feed-meta">
          ${metaBadges}
          ${targets ? `<span class="sim-feed-target">→ ${esc(targets)}</span>` : ''}
        </div>
      </div>`;
    feedEl.appendChild(div);
  });

  // 닫기
  document.getElementById('sim-replay-close-btn').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  // 이어서 실행 — 에이전트 메모리 복원 후 이어서
  const resumeBtn = document.getElementById('sim-replay-resume-btn');
  if (resumeBtn && parsedConfig) {
    resumeBtn.addEventListener('click', async () => {
      resumeBtn.disabled = true;
      resumeBtn.textContent = '복원 중...';
      const res = await fetch(`/api/simulation/resume/${encodeURIComponent(runId)}`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`재개 실패: ${err.detail || '서버 오류'}`);
        resumeBtn.disabled = false;
        resumeBtn.textContent = '↩ 이어서';
        return;
      }
      // 설정 반영 (에이전트 카드 등 UI 동기화)
      applyScenario({ id: run.scenario_id, name: run.scenario_name || '', config: parsedConfig });
      renderAgentCards();
      modal.remove();
      setStatus('running');
      connectSSE();
    });
  }

  // 새로 시작 — 같은 설정으로 처음부터
  const restartBtn = document.getElementById('sim-replay-restart-btn');
  if (restartBtn && parsedConfig) {
    restartBtn.addEventListener('click', () => {
      applyScenario({ id: run.scenario_id, name: run.scenario_name || '', config: parsedConfig });
      renderAgentCards();
      renderSettingsPage();
      modal.remove();
    });
  }
}
