---
name: frontend-skill
description: "vLLM Chat 프론트엔드(HTML, CSS, JavaScript) 개발 가이드. UI 컴포넌트 추가, 스타일 수정, API 연동, SSE 스트리밍 처리 작업 시 참조."
---

# Frontend Development Guide — vLLM Chat

## 핵심 제약 (반드시 준수)

- **빌드 도구 없음** — CDN 라이브러리만. import/require/npm 금지
- **XSS 방어** — innerHTML 직접 할당 금지. `DOMPurify.sanitize()` 또는 DOM API 사용
- **새 JS 파일** — `index.html`에 `<script src="js/new.js">` 추가 필수
- **전역 상태** — `state.js`의 `AppState` 객체 경유

## 주요 패턴

### API 호출 (api.js에 함수 추가)

```javascript
// api.js
export async function fetchNewFeature(params) {
    const res = await fetch(`/api/new-feature?${new URLSearchParams(params)}`);
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}
```

### DOM 요소 생성 (XSS 안전)

```javascript
// 안전한 방법: DOM API
function createMessageEl(text) {
    const div = document.createElement('div');
    div.textContent = text;  // textContent는 자동 이스케이프
    return div;
}

// 마크다운 렌더링이 필요한 경우
function renderMarkdown(text) {
    const html = md.render(text);
    return DOMPurify.sanitize(html);  // 반드시 sanitize
}
```

### 이벤트 리스너 등록 패턴

```javascript
// main.js에서 초기화
document.addEventListener('DOMContentLoaded', () => {
    // 기존 초기화 코드 아래에 추가
    initNewFeature();
});
```

### SSE 스트리밍 (stream.js 패턴 참조)

```javascript
const source = new EventSource(`/api/chat/stream?id=${convId}`);
source.addEventListener('token', (e) => {
    const data = JSON.parse(e.data);
    appendToken(data.content);
});
source.addEventListener('done', () => source.close());
source.onerror = () => source.close();
```

## CSS 시스템

`base.css`의 CSS 변수 활용:
```css
.new-component {
    background: var(--bg-secondary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--border-radius);
}
```

새 컴포넌트 CSS는 해당 기능의 `*.css` 파일에 추가하거나, 독립 기능이면 새 파일 생성 후 `index.html`의 `<link>` 추가.

## 모듈별 책임

| 파일 | 담당 |
|------|------|
| `state.js` | AppState 전역 상태, 선택된 대화/에이전트 등 |
| `api.js` | fetch 래퍼 함수들 (GET/POST/DELETE) |
| `stream.js` | SSE 연결, 토큰 스트리밍, 완료 처리 |
| `messages.js` | 메시지 DOM 생성/추가/업데이트 |
| `markdown.js` | md 인스턴스 생성, renderMarkdown() 노출 |
| `chat.js` | 전송 버튼, 입력 처리, 스트림 시작 |
| `conversations.js` | 사이드바 대화 목록 렌더링 |

## UI 추가 시 체크리스트

1. HTML: `index.html`의 적절한 위치에 마크업 추가
2. CSS: 관련 CSS 파일에 스타일 추가 (CSS 변수 활용)
3. JS: 필요한 fetch 함수를 `api.js`에 추가
4. JS: 컴포넌트 로직을 담당 모듈 또는 새 파일에 구현
5. JS: `main.js`에서 초기화 호출
6. 새 파일이면: `index.html`에 `<script>` 또는 `<link>` 추가
