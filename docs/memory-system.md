# 메모리 시스템

## 개요

컨텍스트 윈도우 한계를 극복하기 위한 RAG(Retrieval-Augmented Generation) 기반 메모리 구조다.  
모든 대화를 LLM에 누적해서 넣는 대신, 오래된 대화를 구조화된 메모리로 변환하고 현재 질문과 관련 있는 것만 선택적으로 주입한다.

## 핵심 아이디어

```
전통적 방식:  [모든 이전 대화] + [현재 질문]  → 컨텍스트 폭발
이 시스템:    [관련 메모리 N건] + [최근 4턴] + [현재 질문]  → 일정한 컨텍스트
```

## DB 스키마

### `memories` 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | TEXT PK | UUID |
| `type` | TEXT | `fact` / `decision` / `pending` |
| `content` | TEXT | 메모리 내용 |
| `created_at` | TEXT | 생성 시각 |
| `last_accessed` | TEXT | 마지막 검색 조회 시각 |

**type 정의:**
- `fact` — 사실, 수치, 설정값, 고유명사
- `decision` — 결정된 사항
- `pending` — 미결 또는 진행 중인 항목

### `memory_keywords` 테이블

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `memory_id` | TEXT FK | `memories.id` 참조 |
| `keyword` | TEXT | 소문자 정규화된 키워드 |

`idx_keyword` 인덱스로 키워드 검색을 가속한다.

### `turns` 테이블 (server.py 관리)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | TEXT PK | UUID |
| `conversation_id` | TEXT | 소속 대화 |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | 메시지 내용 |
| `memories_json` | TEXT | 이 응답에 주입된 메모리 목록 (JSON) |
| `context_pct` | REAL | 응답 시점 컨텍스트 사용률 (0.0~1.0) |
| `prompt_tokens` | INTEGER | 입력 토큰 수 |
| `max_tokens` | INTEGER | 모델 최대 컨텍스트 한도 |
| `archived` | INTEGER | 0: active, 1: 아카이브됨 |

## 메모리 저장 흐름 (아카이브)

컨텍스트 사용률이 `ARCHIVE_THRESHOLD`(75%)를 초과하면 아카이브가 트리거된다.

```
1. active 턴 ID 목록 조회 (오래된 순)
2. 마지막 KEEP_RECENT_TURNS(4)개를 제외한 나머지를 대상으로 선택
3. extract_memories_from_turns(대상 턴들)
   └─ LLM 호출: 대화에서 나중에 참조할 만한 정보 추출
   └─ 반환: [{type, content, keywords}, ...]
4. save_memories(conn, 추출된 메모리들)
   └─ memories 테이블에 INSERT
   └─ memory_keywords 테이블에 키워드별 INSERT
5. 대상 턴들 archived = 1 처리
```

아카이브 이후 LLM에는 최근 4개 active 턴만 전달된다.  
UI에는 아카이브된 메시지도 계속 표시되므로 사용자는 전체 히스토리를 볼 수 있다.

## 메모리 검색 흐름 (RAG)

매 채팅 요청마다 실행된다.

```
1. extract_keywords(사용자 메시지)
   └─ LLM 호출: 핵심 명사·기술 용어 최대 7개 추출
   └─ 반환: ["키워드1", "키워드2", ...]

2. retrieve_memories(conn, keywords, top_k=5)
   └─ SQL:
      SELECT m.id, m.type, m.content, COUNT(mk.keyword) AS match_count
      FROM memories m
      JOIN memory_keywords mk ON m.id = mk.memory_id
      WHERE mk.keyword IN (키워드들)
      GROUP BY m.id
      ORDER BY match_count DESC, m.last_accessed DESC
      LIMIT 5
   └─ 조회된 메모리의 last_accessed 갱신

3. build_messages(system_prompt, retrieved, recent_turns)
   └─ system 메시지에 "[관련 메모리]\n[type] content\n..." 삽입
   └─ 이어서 최근 active 턴들 추가
```

## UI와 LLM 컨텍스트의 차이

아카이브가 발생하면 UI와 LLM이 보는 대화 내용이 달라진다.

```
UI 화면:
  [오래된 메시지 (archived)] ← 사용자에게는 계속 보임
  ──── N개 메시지가 메모리로 저장됨 ────
  [최근 4개 메시지 (active)]

LLM 입력:
  system: [검색된 관련 메모리 N건]
  + [최근 4개 active 메시지]
```

`💡 메모리 N건 참조` 패널에 표시되는 내용 = LLM의 system 프롬프트에 실제로 주입된 메모리.  
아카이브된 전체 내용이 아니라, 현재 질문의 키워드와 교집합이 있는 메모리만 선택적으로 올라온다.

## 메모리 추출 프롬프트 구조

`extract_memories_from_turns`에서 LLM에 전달하는 지시:

```
type 종류:
- fact: 사실, 수치, 설정값, 고유명사
- decision: 결정된 사항
- pending: 미결 또는 진행 중인 항목

keywords는 이 항목을 나중에 검색할 때 쓸 핵심 단어 (최대 5개).
새로운 정보가 없으면 빈 배열 []을 반환.

반환 형식:
[{"type": "fact", "content": "...", "keywords": ["...", "..."]}]
```

## 한계와 개선 방향

| 한계 | 개선 방향 |
|------|-----------|
| 키워드 기반 검색 → 동의어·맥락 미스 | Vector DB로 전환 (의미 기반 유사도 검색) |
| 아카이브 기준이 토큰 수 임계값 고정 | 대화 내용의 중요도 기반 동적 아카이브 |
| 메모리 중복 저장 가능 | 저장 시 유사 메모리 dedup 처리 |
| 오래된 메모리도 동일하게 취급 | time-decay 가중치 적용 |

Vector DB 전환 검토는 [vectordb.md](vectordb.md) 참조.
