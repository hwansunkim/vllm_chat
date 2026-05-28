// frontend/js/sim/runs/history.js
// Run history side panel + the global "all runs" modal.

import { sim, esc } from '../state.js';
import { fmtTime, statusIcon } from '../utils/time.js';
import { openRunReplay } from './replay.js';

export async function toggleRunHistory() {
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel) return;
  const isVisible = !panel.classList.contains('sim-hidden');
  if (isVisible) {
    panel.classList.add('sim-hidden');
    return;
  }
  if (!sim.currentScenarioId) return;
  panel.classList.remove('sim-hidden');
  await refreshRunHistory();
}

export async function refreshRunHistory() {
  if (!sim.currentScenarioId) return;
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel || panel.classList.contains('sim-hidden')) return;

  const res  = await fetch(`/api/simulation/runs?scenario_id=${encodeURIComponent(sim.currentScenarioId)}`);
  const runs = await res.json();

  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) histBtn.textContent = `📋 이력 (${runs.length})`;

  if (!runs.length) {
    panel.innerHTML = '<div class="sim-run-history-empty">아직 실행 이력이 없습니다.</div>';
    return;
  }

  const rows = runs.map(r => `
    <tr>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at)}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td class="rh-view">
        <button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁 보기</button>
      </td>
      <td class="rh-del">
        <button class="sim-run-del-btn" data-run-id="${esc(r.run_id)}">🗑</button>
      </td>
    </tr>`).join('');

  panel.innerHTML = `
    <table class="sim-run-history-table">
      <thead><tr>
        <th>#</th><th>시작</th><th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  panel.querySelectorAll('.sim-run-view-btn').forEach(btn => {
    btn.addEventListener('click', () => openRunReplay(btn.dataset.runId, btn.dataset.runNum));
  });

  panel.querySelectorAll('.sim-run-del-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('이 실행 이력과 메모리 데이터를 삭제하시겠습니까?')) return;
      await fetch(`/api/simulation/runs/${btn.dataset.runId}`, { method: 'DELETE' });
      await refreshRunHistory();
    });
  });
}

export async function openAllRunsModal() {
  document.getElementById('sim-all-runs-modal')?.remove();

  const res = await fetch('/api/simulation/runs');
  if (!res.ok) { alert('이력을 불러오지 못했습니다.'); return; }
  const runs = await res.json();

  const modal = document.createElement('div');
  modal.id = 'sim-all-runs-modal';
  modal.className = 'sim-replay-modal-overlay';

  const rows = runs.length ? runs.map(r => `
    <tr>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at, { includeYear: true })}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px">${esc(r.scenario_name || '—')}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td><button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁 보기</button></td>
      <td><button class="sim-run-del-btn all-modal-del" data-run-id="${esc(r.run_id)}">🗑</button></td>
    </tr>`).join('') :
    '<tr><td colspan="8" style="text-align:center;color:#94a3b8;padding:24px">실행 이력이 없습니다</td></tr>';

  modal.innerHTML = `
    <div class="sim-replay-modal-box" style="width:min(900px,96vw)">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span style="font-size:15px">📋</span> 전체 실행 이력
          <span class="sim-replay-meta">${runs.length}건</span>
        </div>
        <button id="sim-all-runs-close-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">✕ 닫기</button>
      </div>
      <div style="flex:1;overflow-y:auto;padding:12px 16px">
        <table class="sim-run-history-table" style="width:100%">
          <thead><tr>
            <th>#</th><th>시작</th><th>시나리오</th><th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;

  document.body.appendChild(modal);

  document.getElementById('sim-all-runs-close-btn').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  modal.querySelectorAll('.sim-run-view-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      modal.remove();
      openRunReplay(btn.dataset.runId, btn.dataset.runNum);
    });
  });

  modal.querySelectorAll('.all-modal-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('이 실행 이력과 메모리 데이터를 삭제하시겠습니까?')) return;
      await fetch(`/api/simulation/runs/${btn.dataset.runId}`, { method: 'DELETE' });
      modal.remove();
      openAllRunsModal();
    });
  });
}
