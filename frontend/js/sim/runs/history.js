// frontend/js/sim/runs/history.js
// Run history side panel + the global "all runs" modal.

import { sim, esc } from '../state.js';
import { fmtTime, statusIcon } from '../utils/time.js';
import { openRunReplay } from './replay.js';

// ── 일괄 삭제 ──────────────────────────────────────────────────────────────────

async function bulkDelete(runIds) {
  if (!runIds.length) return;
  await fetch('/api/simulation/runs/bulk-delete', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ run_ids: runIds }),
  });
}

// ── 체크박스 헬퍼 ──────────────────────────────────────────────────────────────

function getCheckedIds(container) {
  return [...container.querySelectorAll('.rh-check:checked')].map(cb => cb.dataset.runId);
}

function wireCheckboxes(container, onSelectionChange) {
  const selectAll = container.querySelector('.rh-check-all');

  container.querySelectorAll('.rh-check').forEach(cb => {
    cb.addEventListener('change', () => {
      if (selectAll) {
        const all   = container.querySelectorAll('.rh-check');
        const checked = container.querySelectorAll('.rh-check:checked');
        selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
        selectAll.checked       = checked.length === all.length;
      }
      onSelectionChange(getCheckedIds(container));
    });
  });

  if (selectAll) {
    selectAll.addEventListener('change', () => {
      container.querySelectorAll('.rh-check').forEach(cb => {
        cb.checked = selectAll.checked;
      });
      selectAll.indeterminate = false;
      onSelectionChange(getCheckedIds(container));
    });
  }
}

// ── 사이드 패널 (시나리오별 이력) ─────────────────────────────────────────────

export async function toggleRunHistory() {
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel) return;
  const isVisible = !panel.classList.contains('sim-hidden');
  if (isVisible) { panel.classList.add('sim-hidden'); return; }
  if (!sim.currentScenarioId) return;
  panel.classList.remove('sim-hidden');
  await refreshRunHistory();
}

export async function refreshRunHistory() {
  if (!sim.currentScenarioId) return;
  const panel = document.getElementById('sim-run-history-panel');
  if (!panel || panel.classList.contains('sim-hidden')) return;

  const res  = await fetch(`/api/simulation/runs?scenario_id=${encodeURIComponent(sim.currentScenarioId)}`);
  const runs = res.ok ? await res.json() : [];

  const histBtn = document.getElementById('sim-history-btn');
  if (histBtn) histBtn.textContent = `📋 이력 (${runs.length})`;

  if (!runs.length) {
    panel.innerHTML = '<div class="sim-run-history-empty">아직 실행 이력이 없습니다.</div>';
    return;
  }

  const rows = runs.map(r => `
    <tr>
      <td class="rh-chk-cell">
        <input type="checkbox" class="rh-check" data-run-id="${esc(r.run_id)}">
      </td>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at)}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td class="rh-view">
        <button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁</button>
      </td>
      <td class="rh-del">
        <button class="sim-run-del-btn" data-run-id="${esc(r.run_id)}">🗑</button>
      </td>
    </tr>`).join('');

  panel.innerHTML = `
    <div class="rh-toolbar" id="rh-toolbar-panel">
      <label class="rh-select-all-label">
        <input type="checkbox" class="rh-check-all"> 전체
      </label>
      <button class="rh-bulk-del-btn sim-hidden" id="rh-bulk-del-panel">선택 삭제</button>
    </div>
    <table class="sim-run-history-table">
      <thead><tr>
        <th class="rh-chk-cell"></th>
        <th>#</th><th>시작</th><th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  // 체크박스 — 선택 변화 시 삭제 버튼 표시/숨김
  wireCheckboxes(panel, ids => {
    const btn = document.getElementById('rh-bulk-del-panel');
    if (btn) {
      btn.classList.toggle('sim-hidden', ids.length === 0);
      btn.textContent = ids.length ? `선택 삭제 (${ids.length})` : '선택 삭제';
    }
  });

  document.getElementById('rh-bulk-del-panel')?.addEventListener('click', async () => {
    const ids = getCheckedIds(panel);
    if (!ids.length) return;
    if (!confirm(`선택한 ${ids.length}건의 이력을 삭제하시겠습니까?`)) return;
    await bulkDelete(ids);
    await refreshRunHistory();
  });

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

// ── 전체 이력 모달 ─────────────────────────────────────────────────────────────

export async function openAllRunsModal() {
  document.getElementById('sim-all-runs-modal')?.remove();

  const res = await fetch('/api/simulation/runs');
  if (!res.ok) { alert('이력을 불러오지 못했습니다.'); return; }
  const runs = await res.json();

  const modal = document.createElement('div');
  modal.id    = 'sim-all-runs-modal';
  modal.className = 'sim-replay-modal-overlay';

  const rows = runs.length ? runs.map(r => `
    <tr>
      <td class="rh-chk-cell">
        <input type="checkbox" class="rh-check" data-run-id="${esc(r.run_id)}">
      </td>
      <td class="rh-num">${r.run_number}</td>
      <td class="rh-time">${fmtTime(r.started_at, { includeYear: true })}</td>
      <td class="rh-scenario">${esc(r.scenario_name || '—')}</td>
      <td class="rh-waves">${r.total_waves}</td>
      <td class="rh-turns">${r.total_turns}</td>
      <td class="rh-status">${statusIcon[r.status] || r.status}</td>
      <td><button class="sim-run-view-btn" data-run-id="${esc(r.run_id)}" data-run-num="${r.run_number}">👁 보기</button></td>
      <td><button class="sim-run-del-btn all-modal-del" data-run-id="${esc(r.run_id)}">🗑</button></td>
    </tr>`) .join('') :
    '<tr><td colspan="9" class="rh-empty-cell">실행 이력이 없습니다</td></tr>';

  modal.innerHTML = `
    <div class="sim-replay-modal-box" style="width:min(900px,96vw)">
      <div class="sim-replay-header">
        <div class="sim-replay-title">
          <span style="font-size:15px">📋</span> 전체 실행 이력
          <span class="sim-replay-meta">${runs.length}건</span>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button id="rh-bulk-del-modal" class="rh-bulk-del-btn sim-hidden">선택 삭제</button>
          <button id="sim-all-runs-close-btn" class="sim-ctrl-btn settings" style="font-size:12px;padding:4px 10px">✕ 닫기</button>
        </div>
      </div>
      <div class="rh-modal-toolbar" id="rh-toolbar-modal">
        <label class="rh-select-all-label">
          <input type="checkbox" class="rh-check-all"> 전체 선택
        </label>
      </div>
      <div style="flex:1;overflow-y:auto;padding:0 16px 12px">
        <table class="sim-run-history-table" style="width:100%">
          <thead><tr>
            <th class="rh-chk-cell"></th>
            <th>#</th><th>시작</th><th>시나리오</th>
            <th>Wave</th><th>Turn</th><th>상태</th><th></th><th></th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;

  document.body.appendChild(modal);

  const tableArea = modal.querySelector('[style*="overflow-y"]');

  // 체크박스
  wireCheckboxes(modal, ids => {
    const btn = document.getElementById('rh-bulk-del-modal');
    if (btn) {
      btn.classList.toggle('sim-hidden', ids.length === 0);
      btn.textContent = ids.length ? `선택 삭제 (${ids.length})` : '선택 삭제';
    }
  });

  document.getElementById('rh-bulk-del-modal')?.addEventListener('click', async () => {
    const ids = getCheckedIds(modal);
    if (!ids.length) return;
    if (!confirm(`선택한 ${ids.length}건의 이력을 삭제하시겠습니까?`)) return;
    await bulkDelete(ids);
    modal.remove();
    openAllRunsModal();
  });

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
