# API 레퍼런스

Base URL: `http://localhost:8888`

## 모델 상태

### `GET /api/model/status`

현재 연결된 vLLM 서버와 모델 정보를 반환한다.

**응답**
```json
{
  "model": "google/gemma-4-31B-it",
  "base_url": "http://172.17.3.135:8000",
  "max_model_len": 131072
}
```

---

## 대화 관리

### `GET /api/conversations`

전체 대화 목록을 최근 수정 순으로 반환한다.

**응답**
```json
[
  {
    "id": "uuid",
    "title": "대화 제목",
    "updated_at": "2024-01-01T00:00:00",
    "last_msg": "마지막 메시지 내용"
  }
]
```

---

### `POST /api/conversations`

새 대화를 생성한다.

**요청 바디**
```json
{
  "title": "새 대화",
  "system_prompt": ""
}
```

**응답** `201 Created`
```json
{
  "id": "uuid",
  "title": "새 대화",
  "system_prompt": ""
}
```

---

### `GET /api/conversations/{conv_id}`

대화의 전체 정보와 모든 턴(메시지)을 반환한다.  
아카이브된 턴도 포함된다.

**응답**
```json
{
  "id": "uuid",
  "title": "대화 제목",
  "system_prompt": "",
  "created_at": "2024-01-01T00:00:00",
  "updated_at": "2024-01-01T00:00:00",
  "turns": [
    {
      "role": "user",
      "content": "메시지 내용",
      "memories_json": null,
      "context_pct": null,
      "prompt_tokens": null,
      "max_tokens": null
    },
    {
      "role": "assistant",
      "content": "응답 내용",
      "memories_json": "[{\"type\":\"fact\",\"content\":\"...\"}]",
      "context_pct": 0.1234,
      "prompt_tokens": 1024,
      "max_tokens": 131072
    }
  ]
}
```

---

### `PATCH /api/conversations/{conv_id}/title`

대화 제목을 수동으로 변경한다.

**요청 바디**
```json
{ "title": "새 제목" }
```

**응답**
```json
{ "ok": true }
```

---

### `DELETE /api/conversations/{conv_id}`

대화와 모든 턴을 삭제한다.

**응답** `204 No Content`

---

## 채팅

### `POST /api/conversations/{conv_id}/chat`

메시지를 전송하고 LLM 응답을 받는다.  
내부적으로 RAG 파이프라인(키워드 추출 → 메모리 검색 → LLM 호출 → DB 저장 → 아카이브 검사)이 실행된다.

**요청 바디**
```json
{ "content": "사용자 메시지" }
```

**응답**
```json
{
  "reply": "LLM 응답 텍스트",
  "memories": [
    { "type": "fact", "content": "참조된 메모리 내용" }
  ],
  "usage": {
    "prompt_tokens": 1024,
    "completion_tokens": 256,
    "max_model_len": 131072,
    "context_pct": 0.0078
  },
  "archived_count": 0,
  "title": "자동 생성된 제목"
}
```

| 필드 | 설명 |
|------|------|
| `memories` | 이 응답에 주입된 메모리 목록 (UI의 `💡 메모리 N건 참조`와 동일) |
| `usage.context_pct` | 컨텍스트 사용률 (0.0~1.0). 0.75 초과 시 아카이브 트리거 |
| `archived_count` | 이번 요청에서 아카이브된 턴 수. 0이면 아카이브 없음 |
| `title` | 첫 메시지 시 자동 생성된 제목, 이후에는 기존 제목 반환 |

**에러 응답**
- `404`: 존재하지 않는 `conv_id`
- `502`: vLLM 서버 오류 또는 연결 실패

---

## 자동 제목 생성

첫 번째 메시지 전송 시 제목이 `"새 대화"`인 경우,  
사용자 메시지 앞 30자를 잘라 자동으로 제목을 설정한다.

```python
def auto_title(text: str) -> str:
    return text[:30].strip() + ("..." if len(text) > 30 else "")
```
