import { api } from './api.js';
import { esc } from './utils.js';

let _typeFilter   = '';
let _searchTimer  = null;

export async function openMemoryModal() {
  document.getElementById('mem-modal').classList.remove('hidden');
  await fetchMemories();
}

function closeMemoryModal() {
  document.getElementById('mem-modal').classList.add('hidden');
}

async function fetchMemories() {
  const q      = document.getElementById('mem-search').value.trim();
  const params = new URLSearchParams();
  if (_typeFilter) params.set('type', _typeFilter);
  if (q)           params.set('q', q);

  const list = document.getElementById('mem-list');
  list.innerHTML = '<div class="mem-empty">불러오는 중...</div>';

  try {
    const items = await api('GET', '/memories?' + params.toString());
    renderMemories(items);
  } catch (e) {
    list.innerHTML = '<div class="mem-empty">불러오기 실패: ' + esc(e.message) + '</div>';
  }
}

function renderMemories(items) {
  const list = document.getElementById('mem-list');
  document.getElementById('mem-count').textContent = items.length + '개';

  if (!items.length) {
    list.innerHTML = '<div class="mem-empty">저장된 메모리가 없습니다.</div>';
    return;
  }

  list.innerHTML = items.map(m => {
    const created  = m.created_at.slice(0, 16).replace('T', ' ');
    const accessed = m.last_accessed.slice(0, 16).replace('T', ' ');
    const kwHtml   = m.keywords.map(k => `<span class="mem-kw">${esc(k)}</span>`).join('');
    return `
      <div class="mem-card" data-id="${m.id}">
        <div class="mem-card-top">
          <span class="mem-type-badge ${m.type}">${m.type}</span>
          <span class="mem-content">${esc(m.content)}</span>
          <button class="mem-delete-btn" data-id="${m.id}" title="삭제">✕</button>
        </div>
        ${m.keywords.length ? `<div class="mem-keywords">${kwHtml}</div>` : ''}
        <div class="mem-meta">생성: ${created} · 마지막 참조: ${accessed}</div>
      </div>`;
  }).join('');

  list.addEventListener('click', async e => {
    const btn = e.target.closest('.mem-delete-btn');
    if (btn) await deleteMemory(btn.dataset.id);
  });
}

async function deleteMemory(id) {
  if (!confirm('이 메모리를 삭제하시겠습니까?')) return;
  try {
    await api('DELETE', '/memories/' + id);
    document.querySelector(`.mem-card[data-id="${id}"]`)?.remove();
    const list      = document.getElementById('mem-list');
    const remaining = list.querySelectorAll('.mem-card').length;
    document.getElementById('mem-count').textContent = remaining + '개';
    if (!remaining) list.innerHTML = '<div class="mem-empty">저장된 메모리가 없습니다.</div>';
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

export function initMemoryEvents() {
  document.getElementById('mem-btn').onclick   = openMemoryModal;
  document.getElementById('mem-close').onclick = closeMemoryModal;

  document.getElementById('mem-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('mem-modal')) closeMemoryModal();
  });

  document.querySelectorAll('.mem-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mem-filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _typeFilter = btn.dataset.type;
      fetchMemories();
    });
  });

  document.getElementById('mem-search').addEventListener('input', () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(fetchMemories, 300);
  });
}
