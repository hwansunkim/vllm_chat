/* globals markdownit, texmath, katex, hljs, DOMPurify */

export const md = markdownit({
  html: false,
  linkify: true,
  typographer: false,
  highlight(str, lang) {
    const validLang = lang && hljs.getLanguage(lang) ? lang : null;
    const highlighted = validLang
      ? hljs.highlight(str, { language: validLang, ignoreIllegals: true }).value
      : md.utils.escapeHtml(str);
    const langAttr = validLang ? ` data-lang="${validLang}"` : '';
    return `<pre${langAttr}><code class="hljs${validLang ? ` language-${validLang}` : ''}">${highlighted}</code></pre>`;
  },
}).use(texmath, { engine: katex, delimiters: 'dollars', katexOptions: { throwOnError: false } });

export function renderMarkdown(text) {
  return DOMPurify.sanitize(md.render(text), {
    USE_PROFILES: { html: true, svg: true },
    ADD_ATTR: ['data-lang'],
  });
}

export function highlightCodeBlocks(el) {
  el.querySelectorAll('pre').forEach(pre => {
    const header = document.createElement('div');
    header.className = 'code-header';

    const langSpan = document.createElement('span');
    langSpan.className = 'code-lang';
    langSpan.textContent = pre.dataset.lang || '';
    header.appendChild(langSpan);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'code-copy-btn';
    copyBtn.textContent = '복사';
    copyBtn.addEventListener('click', () => {
      const text = pre.querySelector('code')?.textContent ?? '';
      navigator.clipboard.writeText(text).then(() => {
        copyBtn.textContent = '✓ 복사됨';
        copyBtn.classList.add('copied');
        setTimeout(() => {
          copyBtn.textContent = '복사';
          copyBtn.classList.remove('copied');
        }, 2000);
      });
    });
    header.appendChild(copyBtn);

    pre.insertBefore(header, pre.firstChild);
  });
}
