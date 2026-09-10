# RAG 메모리 시스템 (채팅)

> 채팅 도메인이 컨텍스트 윈도우 한계를 넘기는 방법. 시뮬레이션 에이전트의 메모리
> 압축은 별개다 → [`simulation-engine.md`](simulation-engine.md#6-메모리-압축).
> 파이프라인 맥락은 [`backend.md`](backend.md#2-채팅-파이프라인).

---

## 1. 아이디어

```
전통적 방식:  [모든 이전 대화] + [현재 질문]          → 컨텍스트 폭발
이 시스템:    [관련 메모리 N건] + [최근 4턴] + [현재 질문]  → 일정한 컨텍스트
```

오래된 대화를 **구조화 메모리**로 변환해 저장하고, 현재 질문의 키워드와 겹치는 것만
선택적으로 프롬프트에 주입한다.

**코드 위치** (구 `chat.py`/`server.py`는 없어졌다):

| 관심사 | 위치 |
|---|---|
| 메모리 저장·검색 | `backend/core/memory.py` |
| 키워드·메모리 추출 (LLM) | `backend/llm/pipeline.py` |
| 아카이브 트리거 | `backend/api/_conv_helpers.py: _maybe_archive` |
| 메시지 조립 | `backend/llm/pipeline.py: build_messages` |
| 임계값 상수 | `backend/config.py` |
| 저장소 UI | 메모리 저장소 모달 (`frontend/js/memories.js`) |

---

## 2. 스키마

전체 정의는 [`database.md`](database.md). 요약:

### `memories`

| 컬럼 | 의미 |
|---|---|
| `id` | UUID |
| `type` | `fact` / `decision` / `pending` |
| `content` | 메모리 내용 |
| `created_at` | 생성 시각 |
| `last_accessed` | 마지막 검색 조회 시각 (검색 랭킹의 2차 정렬 키) |

**type 정의** — `fact`: 사실·수치·설정값·고유명사 · `decision`: 결정된 사항 ·
`pending`: 미결 또는 진행 중.

### `memory_keywords`

`memory_id`(FK) · `keyword`(소문자 정규화). `idx_keyword` 인덱스로 검색 가속.

### `turns` (관련 컬럼)

| 컬럼 | 의미 |
|---|---|
| `archived` | `0` active / `1` 아카이브됨 |
| `memories_json` | 이 응답에 주입된 메모리 목록 (JSON) |
| `context_pct` | 응답 시점 컨텍스트 사용률 (0.0~1.0) |
| `prompt_tokens` / `max_tokens` | 입력 토큰 / 모델 컨텍스트 한도 |
| `thinking` | 추론 과정 텍스트 (사고 켜졌을 때) |
| `sources_json` | 웹 검색 출처 (검색 켜졌을 때) |

---

## 3. 검색 흐름 (RAG) — 매 채팅 요청

```
1. async_extract_keywords(user_content)          (pipeline.py, LLM 호출)
     "명사·고유명사·기술 용어 위주 최대 7개, JSON 배열만"
     → ["서버", "포트", "모델이름"]   (실패 시 [])

2. retrieve_memories(conn, keywords, top_k=5)    (core/memory.py)
     SELECT m.id, m.type, m.content, COUNT(mk.keyword) AS match_count
     FROM memories m JOIN memory_keywords mk ON m.id = mk.memory_id
     WHERE mk.keyword IN (?, ?, …)
     GROUP BY m.id
     ORDER BY match_count DESC, m.last_accessed DESC
     LIMIT 5
     → 조회된 메모리의 last_accessed 갱신

3. build_messages(system_prompt, retrieved, recent_turns, web_context)
     system 메시지 =  [페르소나]
                   \n\n[관련 메모리]\n[fact] ...\n[decision] ...
                   \n\n[웹 검색 컨텍스트]   (검색 켜졌을 때)
     그 뒤에 archived=0 인 최근 턴들
```

키워드가 비면 검색을 건너뛴다 (`retrieve_memories`가 `[]` 반환).

---

## 4. 아카이브 흐름 — 컨텍스트 75% 초과 시

응답 저장 직후 `_maybe_archive(conn, conv_id, context_pct, max_model_len)` 실행:

```
조건:  max_model_len 있음  AND  context_pct >= ARCHIVE_THRESHOLD (0.75)

1. active 턴 id 목록 (archived=0, 오래된 순)
2. 마지막 KEEP_RECENT_TURNS(4)개를 제외한 나머지를 대상으로
   (대상이 없으면 종료 → 0 반환)
3. async_extract_memories_from_turns(대상 턴들)     (LLM 호출)
     "나중에 참조할 새 정보 추출, type: fact/decision/pending, keywords 최대 5개"
     → [{"type": "fact", "content": "...", "keywords": [...]}, ...]  (없으면 [])
     ⚠ LLM 호출 실패·응답 잘림·형식 위반이면 MemoryExtractionError 를 던진다.
        _maybe_archive 는 이 예외를 잡아 **아카이브를 통째로 건너뛰고 0 을 반환**한다
        (원문은 archived=0 유지 → 다음 응답 때 재시도, 그 사이에도 최근 맥락으로 쓰임).
        "새 정보 없음"(정상 빈 배열)과 반드시 구분된다.
4. save_memories(conn, 추출 결과, commit=False)
     memories INSERT + 키워드별 memory_keywords INSERT (소문자)
5. 대상 턴들 archived = 1
6. 4·5 를 한 트랜잭션으로 커밋 (부분 상태 방지: "메모리만 저장 / 원문만 삭제" 없음)
7. 아카이브된 턴 수 반환 → done 이벤트의 archived_count
```

아카이브 이후 LLM에는 **최근 4개 active 턴 + 검색된 메모리**만 전달된다.

---

## 5. UI와 LLM 컨텍스트의 차이

```
브라우저 화면 (#messages):                LLM 입력:
  [오래된 메시지 (archived=1)]  ← 계속 보임   system: [검색된 관련 메모리 N건]
  ──── N개 메시지가 메모리로 저장됨 ────       +  [최근 4개 active 턴]
  [최근 4개 메시지 (archived=0)]
```

- `💡 메모리 N건 참조` 패널 = LLM system 프롬프트에 **실제로 주입된** 메모리
  (`turn.memories_json`으로 영속). 아카이브된 전체가 아니라 현재 질문 키워드와
  교집합이 있는 것만.
- 대화를 다시 열면 `conversations.js`가 `memories_json`을 복원해 같은 패널을 그린다.

---

## 6. 한계와 개선 방향

| 한계 | 개선 방향 |
|---|---|
| 키워드 정확 매칭 → 동의어·맥락 미스 | Vector DB (의미 유사도) → [`vectordb.md`](vectordb.md) |
| 아카이브 기준이 토큰 임계값 고정 | 중요도 기반 동적 아카이브 |
| 메모리 중복 저장 가능 | 저장 시 유사 메모리 dedup |
| 오래된 메모리도 동일 취급 | time-decay 가중치 |
