// frontend/js/sim/settings/output-fields.js
// Editor for the configurable per-agent output metadata fields.

import { sim, esc } from '../state.js';
import { updateSectionBadges } from './sections.js';

export function renderOutputFields() {
  const list = document.getElementById('sim-fields-list');
  if (!list) return;
  list.innerHTML = '';
  updateSectionBadges(sim);   // 필드 개수가 섹션 헤더 뱃지에 그대로 노출된다

  if (!sim.extra_fields.length) {
    list.innerHTML = '<div class="sim-fields-empty">메타데이터 필드 없음 — content, target만 사용됩니다.</div>';
    return;
  }

  sim.extra_fields.forEach((f, idx) => {
    const isActionNote = f.name === 'action_note';
    const row = document.createElement('div');
    row.className = `sim-field-row${isActionNote ? ' sim-field-row-action-note' : ''}`;
    row.innerHTML = isActionNote
      ? `
        <span class="sim-field-name sim-field-name-locked" title="행동 묘사 전용 필드">action_note</span>
        <span class="sim-field-sep">:</span>
        <span class="sim-field-action-note-hint">행동 묘사 (피드·컨텍스트에 별도 표시)</span>
        <button class="sim-field-del" data-idx="${idx}">✕</button>
      `
      : `
        <input class="sim-field-name" type="text" placeholder="필드명 (영문)"
               value="${esc(f.name)}" data-idx="${idx}" data-prop="name"/>
        <span class="sim-field-sep">:</span>
        <input class="sim-field-default" type="text" placeholder="기본값"
               value="${esc(f.default)}" data-idx="${idx}" data-prop="default"/>
        <button class="sim-field-del" data-idx="${idx}">✕</button>
      `;
    list.appendChild(row);

    if (!isActionNote) {
      row.querySelectorAll('[data-prop]').forEach(el => {
        el.addEventListener('input', () => {
          sim.extra_fields[+el.dataset.idx][el.dataset.prop] = el.value;
        });
      });
    }
    row.querySelector('.sim-field-del').addEventListener('click', () => {
      sim.extra_fields.splice(idx, 1);
      renderOutputFields();
    });
  });
}
