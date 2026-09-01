// frontend/js/sim/resize.js
// Drag-to-resize handle wiring for the right-side panel.

export function initResizeHandles() {
  setupResize(
    document.getElementById('sim-resize-r'),
    document.getElementById('sim-graph-panel'),
    'left'
  );
}

function setupResize(handle, panel, growDir) {
  if (!handle || !panel) return;
  handle.addEventListener('mousedown', e => {
    const startX = e.clientX;
    const startW = panel.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = ev => {
      const dx   = growDir === 'right' ? ev.clientX - startX : startX - ev.clientX;
      const newW = Math.max(160, Math.min(1000, startW + dx));
      panel.style.width    = `${newW}px`;
      panel.style.minWidth = `${newW}px`;
    };
    const onUp = () => {
      handle.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
    e.preventDefault();
  });
}
