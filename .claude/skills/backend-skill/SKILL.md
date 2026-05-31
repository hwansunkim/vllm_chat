---
name: backend-skill
description: "vLLM Chat 백엔드(FastAPI, SQLite, LLM 파이프라인) 개발 가이드. FastAPI 엔드포인트 추가/수정, DB 스키마 변경, LLM 연동 수정, RAG 메모리 시스템 작업 시 참조."
---

# Backend Development Guide — vLLM Chat

## 핵심 패턴

### 새 API 엔드포인트 추가

```python
# backend/api/new_feature.py
from fastapi import APIRouter, Depends
from ..db.database import get_db
from .schemas import NewFeatureResponse

router = APIRouter(prefix="/api/new-feature", tags=["new-feature"])

@router.get("/", response_model=NewFeatureResponse)
async def get_new_feature():
    conn = get_db()
    try:
        # 로직
        pass
    finally:
        conn.close()
```

`backend/main.py`에 등록:
```python
from .api import new_feature
app.include_router(new_feature.router)
```

### DB 스키마 변경 (하위호환 마이그레이션)

`backend/db/database.py`의 `migrate_db()` 함수에 추가:
```python
def migrate_db(conn):
    # 기존 마이그레이션 ...
    
    # 새 컬럼 추가 (하위호환)
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN new_field TEXT")
        conn.commit()
    except Exception:
        pass  # 이미 존재하면 무시
```

### LLM 호출

```python
from ..llm.client import async_llm, async_stream_llm

# 단일 응답
result = await async_llm(prompt, max_tokens=1000, temperature=0.7)

# 스트리밍 (SSE)
async for chunk in async_stream_llm(messages):
    yield chunk
```

### Pydantic 스키마 추가

`backend/api/schemas.py`에 추가:
```python
class NewFeatureRequest(BaseModel):
    field: str
    optional_field: str | None = None

class NewFeatureResponse(BaseModel):
    id: int
    result: str
```

## 아키텍처 원칙

| 원칙 | 구체적 방법 |
|------|-----------|
| DB 접근 | 항상 `get_db()` + finally close |
| 비동기 | async/await 기본, sync는 `run_in_executor` |
| 에러 응답 | `HTTPException(status_code=xxx, detail="...")` |
| 상태 공유 | `backend/state.py`의 전역 변수 |
| 설정 | `backend/config.py` 환경변수 |

## RAG 메모리 흐름

```
사용자 메시지
    → extract_keywords() (LLM 호출, 최대 7개 명사)
    → retrieve_memories() (키워드 교집합 COUNT + 최근접근 정렬)
    → build_messages() ([메모리 시스템 프롬프트] + 최근 턴)
    → LLM 호출
    → save_turn() (user/assistant 턴 + 메모리 저장)
```

## 에이전트 라우팅

- **멘션 방식**: `@agent_name` 접두사 → `resolve_agent_mention()` → 해당 에이전트 시스템 프롬프트 주입
- **자동 라우팅**: 멘션 없고 에이전트 여러 개 → `async_route_agent()` → LLM이 가장 적합한 에이전트 선택

## 주요 파일 참조 포인터

상세 구현이 필요한 경우:
- LLM 프로바이더 추가: `backend/llm/providers/` + `registry.py`
- 메모리 검색 알고리즘: `backend/core/memory.py`
- 스트리밍 파이프라인: `backend/llm/pipeline.py`
