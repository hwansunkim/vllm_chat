import { state } from './state.js';
import { esc } from './utils.js';

let _selectedIdx = -1;

function showMentionDropdown(query) {
  const matches = state.agentList.filter(a => a.name.toLowerCase().includes(query.toLowerCase()));
  const dd = document.getElementById('mention-dropdown');
  if (!matches.length) { dd.classList.add('hidden'); return; }
  _selectedIdx = -1;
  dd.innerHTML = matches.map((a, i) => `
    <div class="mention-item" data-name="${a.name}" data-idx="${i}">
      <span>${esc(a.icon)}</span>
      <div>
        <div class="mention-item-name">${esc(a.name)}</div>
        <div class="mention-item-desc">${esc(a.description || a.role || '')}</div>
      </div>
    </div>`).join('');
  dd.querySelectorAll('.mention-item').forEach(el => {
    el.addEventListener('mousedown', e => {
      e.preventDefault();
      completeMention(el.dataset.name);
    });
  });
  dd.classList.remove('hidden');
}

function hideMentionDropdown() {
  document.getElementById('mention-dropdown').classList.add('hidden');
  _selectedIdx = -1;
}

function completeMention(name) {
  const input  = document.getElementById('message-input');
  const before = input.value.slice(0, input.selectionStart);
  const after  = input.value.slice(input.selectionStart);
  const newBefore = before.replace(/@\S*$/, `@${name} `);
  input.value = newBefore + after;
  input.selectionStart = input.selectionEnd = newBefore.length;
  hideMentionDropdown();
  input.focus();
}

export function initMentionEvents() {
  const input = document.getElementById('message-input');

  input.addEventListener('input', function () {
    const before = this.value.slice(0, this.selectionStart);
    const m = before.match(/@(\S*)$/);
    if (m) showMentionDropdown(m[1]);
    else   hideMentionDropdown();
  });

  input.addEventListener('keydown', e => {
    const dd = document.getElementById('mention-dropdown');
    if (dd.classList.contains('hidden')) return;
    const items = dd.querySelectorAll('.mention-item');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _selectedIdx = Math.min(_selectedIdx + 1, items.length - 1);
      items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _selectedIdx = Math.max(_selectedIdx - 1, 0);
      items.forEach((el, i) => el.classList.toggle('selected', i === _selectedIdx));
    } else if (e.key === 'Enter' && _selectedIdx >= 0) {
      e.preventDefault();
      completeMention(items[_selectedIdx].dataset.name);
    } else if (e.key === 'Escape') {
      hideMentionDropdown();
    }
  });
}
