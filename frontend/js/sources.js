// ── 웹 검색 출처 블록 (공통 렌더러) ─────────────────────────────────────────────
// stream.js (실시간 search 이벤트) 와 messages.js (저장된 대화 재로드) 가 공유한다.
// 모든 외부 문자열은 DOM API(textContent) 로만 넣어 XSS 를 차단한다.

function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch (e) {
    return '';
  }
}

/**
 * @param {string|null} query   재작성된 검색어 (없으면 헤더에서 생략)
 * @param {Array<{title?:string,url?:string,snippet?:string}>} results
 * @returns {HTMLElement} .source-refs 블록
 */
export function renderSourceRefs(query, results) {
  results = Array.isArray(results) ? results : [];

  const box = document.createElement('div');
  box.className = 'source-refs';

  const header = document.createElement('div');
  header.className = 'source-query';
  header.textContent = query ? `🔍 웹 검색: ${query}` : '🔍 웹 검색 출처';
  box.appendChild(header);

  if (results.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'source-empty';
    empty.textContent = '검색 결과 없음';
    box.appendChild(empty);
    return box;
  }

  const toggle = document.createElement('button');
  toggle.className = 'source-toggle';
  toggle.textContent = `📄 출처 ${results.length}건 ▾`;

  const list = document.createElement('div');
  list.className = 'source-list';

  results.forEach((r, i) => {
    const item = document.createElement('div');
    item.className = 'source-item';

    const rawUrl = r && r.url ? String(r.url) : '';
    const safeUrl = /^https?:\/\//i.test(rawUrl) ? rawUrl : null;

    const a = document.createElement('a');
    a.className = 'source-title';
    a.textContent = `[${i + 1}] ${(r && r.title) || rawUrl || '(제목 없음)'}`;
    a.href = safeUrl || '#';
    if (safeUrl) {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    }
    item.appendChild(a);

    if (r && r.snippet) {
      const snip = document.createElement('div');
      snip.className = 'source-snippet';
      snip.textContent = r.snippet;
      item.appendChild(snip);
    }

    const dom = domainOf(rawUrl);
    if (dom) {
      const d = document.createElement('div');
      d.className = 'source-domain';
      d.textContent = dom;
      item.appendChild(d);
    }

    list.appendChild(item);
  });

  toggle.onclick = () => {
    const open = list.classList.toggle('open');
    toggle.textContent = `📄 출처 ${results.length}건 ${open ? '▴' : '▾'}`;
  };

  box.appendChild(toggle);
  box.appendChild(list);
  return box;
}
