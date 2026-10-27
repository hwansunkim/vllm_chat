// 발표 진행: 단계(data-step)를 하나씩 넘기고, 단계가 끝나면 다음 장으로.
(() => {
  const stage = document.querySelector('.stage');
  const slides = [...document.querySelectorAll('.slide')];
  const bar = document.querySelector('.progress');
  const notesPanel = document.querySelector('.notes-panel');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const total = slides.length;

  // 무대 크기 맞춤
  function fit() {
    const s = Math.min(innerWidth / 1920, innerHeight / 1080);
    stage.style.transform = `translate(-50%, -50%) scale(${s})`;
  }
  addEventListener('resize', fit); fit();

  // 쪽 번호
  slides.forEach((s, k) => {
    const n = document.createElement('div');
    n.className = 'pageno';
    n.textContent = `${String(k + 1).padStart(2, '0')} / ${total}`;
    if (!s.classList.contains('no-pageno')) s.appendChild(n);
  });

  // 선 그리기 길이
  document.querySelectorAll('.draw').forEach(p => {
    try { p.style.setProperty('--len', Math.ceil(p.getTotalLength()) + 1); } catch (e) {}
  });

  // 토큰 블록은 하나씩 차례로 나타난다
  document.querySelectorAll('.blocks').forEach(b => [...b.children].forEach((c, k) => c.style.transitionDelay = `${k * 40}ms`));

  // 타이핑할 글을 보관
  document.querySelectorAll('[data-type]').forEach(el => { el.dataset.full = el.textContent; el.textContent = ''; });

  const maxStep = s => Math.max(0, ...[...s.querySelectorAll('[data-step],[data-at]')]
    .map(e => +(e.dataset.step || e.dataset.at.split(':')[0])));

  let i = 0, step = 0;
  const timers = new Set();

  function type(el) {
    if (el.dataset.done === '1' || el.dataset.busy === '1') return;
    const full = el.dataset.full;
    if (reduced) { el.textContent = full; el.dataset.done = '1'; return; }
    el.dataset.busy = '1'; el.classList.add('typing');
    const speed = +(el.dataset.type || 38);
    let k = 0;
    const tick = () => {
      k += 1; el.textContent = full.slice(0, k);
      if (k < full.length) { const t = setTimeout(tick, speed); timers.add(t); }
      else { el.classList.remove('typing'); el.dataset.busy = '0'; el.dataset.done = '1'; }
    };
    const t = setTimeout(tick, +(el.dataset.delay || 250)); timers.add(t);
  }
  function resetType(el) { el.textContent = ''; el.dataset.done = '0'; el.dataset.busy = '0'; el.classList.remove('typing'); }

  function render() {
    timers.forEach(clearTimeout); timers.clear();
    slides.forEach((s, k) => {
      s.classList.toggle('is-current', k === i);
      s.classList.toggle('is-past', k < i);
      const cur = k === i ? step : (k < i ? Infinity : 0);
      s.querySelectorAll('[data-step]').forEach(el => el.classList.toggle('on', +el.dataset.step <= cur));
      s.querySelectorAll('[data-out]').forEach(el => el.classList.toggle('gone', +el.dataset.out <= cur));
      s.querySelectorAll('[data-at]').forEach(el => {            // "n:class" 단계에서 클래스 추가
        el.dataset.at.split(' ').forEach(pair => {
          const [n, cls] = pair.split(':'); el.classList.toggle(cls, +n <= cur);
        });
      });
      s.querySelectorAll('[data-type]').forEach(el => {
        const owner = el.closest('[data-step]');
        const visible = k === i && (!owner || owner.classList.contains('on'));
        if (visible) type(el); else resetType(el);
      });
    });
    bar.style.width = `${((i + (step / Math.max(1, maxStep(slides[i])))) / (total - 1)) * 100}%`;
    const n = slides[i].querySelector('.notes');
    notesPanel.innerHTML = n ? n.innerHTML : '';
    history.replaceState(null, '', `#${i + 1}`);
  }

  function next() {
    if (step < maxStep(slides[i])) step += 1;
    else if (i < total - 1) { i += 1; step = 0; }
    render();
  }
  function prev() {
    if (step > 0) step -= 1;
    else if (i > 0) { i -= 1; step = maxStep(slides[i]); }
    render();
  }
  function go(k) { i = Math.max(0, Math.min(total - 1, k)); step = 0; render(); }

  addEventListener('keydown', e => {
    if (['ArrowRight', 'PageDown', ' ', 'Enter'].includes(e.key)) { e.preventDefault(); next(); }
    else if (['ArrowLeft', 'PageUp', 'Backspace'].includes(e.key)) { e.preventDefault(); prev(); }
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(total - 1);
    else if (e.key === 'n' || e.key === 'N') document.body.classList.toggle('show-notes');
    else if (e.key === 'g' || e.key === 'G') document.body.classList.toggle('show-grid');
    else if (e.key === 'f' || e.key === 'F') {
      if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen();
    }
  });
  addEventListener('click', e => { if (!e.target.closest('a, button')) (e.clientX < innerWidth * 0.25 ? prev : next)(); });

  const fromHash = parseInt(location.hash.slice(1), 10);
  if (fromHash >= 1 && fromHash <= total) i = fromHash - 1;
  // ?final 이면 그 장의 마지막 단계로 연다(점검·인쇄용)
  if (new URLSearchParams(location.search).has('final')) step = maxStep(slides[i]);
  // 첫 그리기 전에 전환 효과 없이 위치를 잡는다
  slides.forEach(s => s.style.transition = 'none');
  render();
  requestAnimationFrame(() => requestAnimationFrame(() => slides.forEach(s => s.style.transition = '')));
})();
