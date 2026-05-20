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
  el.querySelectorAll('pre[data-lang]').forEach(pre => {
    const label = document.createElement('span');
    label.className = 'code-lang';
    label.textContent = pre.dataset.lang;
    pre.appendChild(label);
  });
}
