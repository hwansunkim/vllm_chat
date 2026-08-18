// frontend/js/sim/runs/replay.js
// Run replay modal: read-only feed + resume / restart actions.

import { sim, esc, emotionClass, agentLabel } from '../state.js';
import { fmtTime, statusIcon } from '../utils/time.js';
import { applyScenario } from '../scenarios.js';
import { setStatus } from '../run/control.js';
import { renderAgentCards } from '../run/cards.js';
import { renderHistoricalFeed } from '../run/feed.js';
import { initD3Graph } from '../graph/d3.js';
import { updateScenarioLabel } from '../views.js';
import { connectSSE } from '../run/sse.js';
import { exportRunMarkdown } from '../export/markdown.js';
import { openInterviewPanel, isInterviewable, closeInterviewPanel } from './interview.js';

export async function openRunReplay(runId, runNum) {
  // 기존 모달 제거 — 인터뷰 패널도 함께 닫는다.
  // (남겨 두면 이전 run_id 를 가리키는 패널이 새 리플레이 위에 떠 있게 된다.)
  document.getElementById('sim-replay-modal')?.remove();
  closeInterviewPanel();

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

  // 인터뷰 진입점 — config 스냅샷이 있고(없으면 API가 400) 종료된 실행일 때만(아니면 409).
  // 진행 중인 실행에서는 버튼을 비활성 상태로 남겨 왜 못 하는지 알 수 있게 한다.
  const canInterview   = !!parsedConfig && isInterviewable(run);
  const showInterview  = !!parsedConfig;
  const interviewTitle = canInterview
    ? '이 실행의 에이전트에게 사후 질문하기'
    : '시뮬레이션이 진행 중입니다. 종료된 뒤에 인터뷰할 수 있습니다.';

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
          ${showInterview ? `<button id="sim-replay-interview-btn" class="sim-ctrl-btn settings sim-itv-open-btn" style="font-size:12px;padding:4px 10px" title="${esc(interviewTitle)}" ${canInterview ? '' : 'disabled'}>🎤 인터뷰</button>` : ''}
          ${parsedConfig ? `<button id="sim-replay-resume-btn" class="sim-ctrl-btn continue" style="font-size:12px;padding:4px 10px">↩ 이어서</button>` : ''}
          ${parsedConfig ? `<button id="sim-replay-restart-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">이력 불러오기</button>` : ''}
          ${log.length   ? `<button id="sim-replay-md-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">📥 MD</button>` : ''}
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

  // MD 내보내기
  document.getElementById('sim-replay-md-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('sim-replay-md-btn');
    if (btn) { btn.disabled = true; btn.textContent = '...'; }
    try {
      await exportRunMarkdown(runId, run, log);
    } catch (e) {
      console.error('[replay] MD 내보내기 실패:', e);
      alert(`MD 내보내기 실패: ${e.message}`);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '📥 MD'; }
    }
  });

  // 인터뷰 — 리플레이 모달 위에 겹쳐 연다 (리플레이는 그대로 유지)
  if (canInterview) {
    document.getElementById('sim-replay-interview-btn')?.addEventListener('click', () => {
      openInterviewPanel(runId, runNum, run, parsedConfig.agents);
    });
  }

  // 닫기 — 위에 겹쳐 뜬 인터뷰 패널도 함께 정리한다.
  const closeReplay = () => { closeInterviewPanel(); modal.remove(); };
  document.getElementById('sim-replay-close-btn').addEventListener('click', closeReplay);
  modal.addEventListener('click', e => { if (e.target === modal) closeReplay(); });

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
      closeReplay();
      setStatus('running');
      connectSSE();
    });
  }

  // 이력 불러오기 — 에이전트 메모리·피드 복원 후 '이어서' 준비 상태로
  const restartBtn = document.getElementById('sim-replay-restart-btn');
  if (restartBtn && parsedConfig) {
    restartBtn.addEventListener('click', async () => {
      restartBtn.disabled = true;
      restartBtn.textContent = '불러오는 중...';

      const res = await fetch(`/api/simulation/load/${encodeURIComponent(runId)}`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert(`불러오기 실패: ${err.detail || '서버 오류'}`);
        restartBtn.disabled = false;
        restartBtn.textContent = '이력 불러오기';
        return;
      }
      const data = await res.json();

      // 시나리오 설정 반영 (sim.* 상태 업데이트)
      applyScenario({ id: run.scenario_id, name: run.scenario_name || '', config: parsedConfig });

      // 에이전트 카드 · 그래프 초기화
      renderAgentCards();
      initD3Graph();

      // 과거 대화 피드 복원
      renderHistoricalFeed(data.log);

      // 시뮬레이션 뷰 표시 (설정창 닫기)
      document.getElementById('sim-settings-view').classList.add('sim-hidden');
      document.getElementById('sim-view').classList.remove('sim-hidden');
      updateScenarioLabel();

      // status='done' → '이어서' 버튼 활성화
      setStatus('done');

      modal.remove();
    });
  }
}
