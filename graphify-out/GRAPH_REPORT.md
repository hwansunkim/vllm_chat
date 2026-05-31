# Graph Report - vllm_chat  (2026-05-26)

## Corpus Check
- 79 files · ~28,670 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 616 nodes · 1091 edges · 45 communities (42 shown, 3 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 49 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `0bca5cc2`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]

## God Nodes (most connected - your core abstractions)
1. `get_db()` - 34 edges
2. `esc()` - 26 edges
3. `Agent` - 21 edges
4. `api()` - 20 edges
5. `get_registry()` - 18 edges
6. `VLLMProvider` - 17 edges
7. `ChatMessage` - 16 edges
8. `Simulation` - 16 edges
9. `ServerRegistry` - 14 edges
10. `str` - 13 edges

## Surprising Connections (you probably didn't know these)
- `FakeStreamResponse` --uses--> `ChatMessage`  [INFERRED]
  tests/test_regressions.py → backend/api/schemas.py
- `FakeHTTPClient` --uses--> `ChatMessage`  [INFERRED]
  tests/test_regressions.py → backend/api/schemas.py
- `FakeProvider` --uses--> `ChatMessage`  [INFERRED]
  tests/test_regressions.py → backend/api/schemas.py
- `FakeProvider` --uses--> `VLLMProvider`  [INFERRED]
  tests/test_regressions.py → backend/llm/providers/vllm.py
- `FakeRegistry` --uses--> `ChatMessage`  [INFERRED]
  tests/test_regressions.py → backend/api/schemas.py

## Communities (45 total, 3 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.08
Nodes (57): closeAgentModal(), deleteAgent(), hideAgentForm(), initAgentEvents(), loadAgents(), openAgentModal(), renderAgents(), saveAgent() (+49 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (54): AgentCreate, AgentUpdate, create_agent(), delete_agent(), get_agent(), list_agents(), update_agent(), list_conversations() (+46 more)

### Community 2 - "Community 2"
Cohesion: 0.08
Nodes (50): auto_title(), create_conversation(), delete_conversation(), get_active_turns(), get_conversation(), _maybe_archive(), _resolve_routing(), save_turn() (+42 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (32): Agent, _build_output_format(), _estimate_tokens(), _msg_tokens(), int, str, Estimate total prompt tokens for the next LLM call., Remove oldest memory messages until estimated tokens fit within _token_limit. (+24 more)

### Community 4 - "Community 4"
Cohesion: 0.07
Nodes (41): addFeedMessage(), addSceneEventToFeed(), addTypingIndicator(), applyScenario(), connectSSE(), _d3Data, deleteScenario(), EMOTION_CLASS (+33 more)

### Community 5 - "Community 5"
Cohesion: 0.11
Nodes (13): bool, float, int, str, Exception, _ContextLengthRetry, _extract_reply(), _parse_context_error() (+5 more)

### Community 6 - "Community 6"
Cohesion: 0.08
Nodes (20): 2-A. 풀스택 피처 개발 (에이전트 팀), 2-B. 백엔드/프론트엔드 단독 작업 (서브 에이전트), 2-C. ABM 작업 (서브 에이전트), 2-D. QA/버그 조사 (서브 에이전트), code:block2 (TaskCreate(tasks: [), code:block6 ([오케스트레이터]), Phase 0: 컨텍스트 확인 (후속 작업 지원), Phase 1: 작업 분류 및 준비 (+12 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (22): API 레퍼런스, code:json ({), code:python (def auto_title(text: str) -> str:), code:json ([), code:json ({), code:json ({), code:json ({), code:json ({ "title": "새 제목" }) (+14 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (17): code:block1 (전통적 방식:  [모든 이전 대화] + [현재 질문]  → 컨텍스트 폭발), code:block2 (1. active 턴 ID 목록 조회 (오래된 순)), code:block3 (1. extract_keywords(사용자 메시지)), code:block4 (UI 화면:), code:block5 (type 종류:), DB 스키마, `memories` 테이블, `memory_keywords` 테이블 (+9 more)

### Community 9 - "Community 9"
Cohesion: 0.11
Nodes (17): code:bash (pip install graphifyy && graphify install   # Claude Code에 등), code:bash (pip install graphifyy[pdf]    # PDF 추출), code:bash (/graphify .                   # 현재 디렉토리 전체 분석), code:bash (/graphify query "질문 내용"           # 의미 기반 검색 (기본 BFS, --dfs ), code:bash (graphify export callflow-html         # 아키텍처 다이어그램 생성), code:bash (# 전체 코드베이스를 그래프로 변환), Graphify — 지식 그래프 빌더, vLLM Chat 프로젝트 활용 예시 (+9 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (16): Backend Development Guide — vLLM Chat, code:python (# backend/api/new_feature.py), code:python (from .api import new_feature), code:python (def migrate_db(conn):), code:python (from ..llm.client import async_llm, async_stream_llm), code:python (class NewFeatureRequest(BaseModel):), code:block6 (사용자 메시지), DB 스키마 변경 (하위호환 마이그레이션) (+8 more)

### Community 11 - "Community 11"
Cohesion: 0.21
Nodes (6): Connection, str, 서버 풀을 관리하고 요청별로 최적 서버를 선택한다.      선택 우선순위:       1. server_id 명시 → 해당 서버 (enable, ServerRegistry, LLMProvider, VLLMProvider

### Community 12 - "Community 12"
Cohesion: 0.12
Nodes (15): API 호출 (api.js에 함수 추가), code:javascript (// api.js), code:javascript (// 안전한 방법: DOM API), code:javascript (// main.js에서 초기화), code:javascript (const source = new EventSource(`/api/chat/stream?id=${convId), code:css (.new-component {), CSS 시스템, DOM 요소 생성 (XSS 안전) (+7 more)

### Community 13 - "Community 13"
Cohesion: 0.14
Nodes (12): ABM API 엔드포인트, ABM Development Guide — vLLM Chat, code:block1 (Wave 0: start_agent가 발화 → targets=[agent_b, agent_c]), code:json ({), code:python (# ABM/scenarios/my_scenario.py), code:python (from .my_scenario import build_scenario as my_scenario), extra_fields 활용 (에이전트별 커스텀 메타), 새 시나리오 생성 (convenience_store.py 참고) (+4 more)

### Community 14 - "Community 14"
Cohesion: 0.24
Nodes (6): bool, float, int, str, Protocol, LLMProvider

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (11): ABM Engineer — 멀티에이전트 시뮬레이션 전담, code:block1 (ABM/), code:python (from ABM.agent import Agent), 시나리오 파일 구조, 시뮬레이션 핵심 개념, 에러 핸들링, 입력/출력 프로토콜, 작업 원칙 (+3 more)

### Community 16 - "Community 16"
Cohesion: 0.18
Nodes (10): CDN 라이브러리 (index.html에 이미 포함), code:block1 (frontend/), Frontend Developer — HTML / CSS / JavaScript 전담, 에러 핸들링, 입력/출력 프로토콜, 작업 원칙, 팀 통신 프로토콜, 프로젝트 구조 (프론트엔드) (+2 more)

### Community 17 - "Community 17"
Cohesion: 0.18
Nodes (10): QA Reviewer — 코드 리뷰 / 버그 탐지 / 통합 정합성 검증, 검증 우선순위, 검증 워크플로우, 경계면 버그 패턴 (반드시 확인), 에러 핸들링, 입력/출력 프로토콜, 작업 원칙, 코드 리뷰 체크리스트 (+2 more)

### Community 18 - "Community 18"
Cohesion: 0.18
Nodes (10): `chat.py` — 핵심 로직 모듈, code:block1 (┌─────────────────────────────────────────────────────────┐), code:block2 (사용자 메시지), `server.py` — FastAPI 웹 서버, `static/index.html` — 단일 파일 프론트엔드, 데이터 흐름 — 채팅 1회 요청, 아키텍처, 전체 구조 (+2 more)

### Community 19 - "Community 19"
Cohesion: 0.20
Nodes (9): Backend Developer — FastAPI / SQLite / LLM 파이프라인 전담, code:block1 (backend/), 에러 핸들링, 입력/출력 프로토콜, 작업 원칙, 팀 통신 프로토콜, 프로젝트 구조 (백엔드), 핵심 역할 (+1 more)

### Community 20 - "Community 20"
Cohesion: 0.29
Nodes (6): 1. 임베딩 모델(Embedding Model)의 선택, 2. 데이터 청킹 전략 (Chunking Strategy), 3. 검색 및 재구성 로직 (RAG Pipeline), 4. Vector DB 엔진 선택, 5. SQLite와의 공존 (Hybrid Storage), 🚀 아담의 제안: 단계적 전환 로드맵

### Community 21 - "Community 21"
Cohesion: 0.33
Nodes (5): code:bash (pip install -r requirements.txt), code:bash (uvicorn backend.main:app --host 0.0.0.0 --port 8888 --reload), vLLM Web Chat, 문서, 실행

### Community 22 - "Community 22"
Cohesion: 0.60
Nodes (4): bool, check_server(), main(), stream_chat()

### Community 23 - "Community 23"
Cohesion: 0.60
Nodes (4): check_server(), main(), bool, stream_chat()

### Community 24 - "Community 24"
Cohesion: 0.50
Nodes (3): str, 시스템에서 한글 폰트 파일을 찾아 matplotlib에 등록하고 기본 폰트로 설정.     - 파일명 기반 탐색으로 폰트 이름 불일치 문제를 피, setup_korean_font()

## Knowledge Gaps
- **138 isolated node(s):** `str`, `bool`, `harness@harness-marketplace`, `allow`, `bool` (+133 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **3 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `get_db()` connect `Community 1` to `Community 2`?**
  _High betweenness centrality (0.058) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 3` to `Community 1`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Why does `Simulation` connect `Community 3` to `Community 1`?**
  _High betweenness centrality (0.028) - this node is a cross-community bridge._
- **Are the 7 inferred relationships involving `Agent` (e.g. with `Simulation` and `str`) actually correct?**
  _`Agent` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `str`, `시스템에서 한글 폰트 파일을 찾아 matplotlib에 등록하고 기본 폰트로 설정.     - 파일명 기반 탐색으로 폰트 이름 불일치 문제를 피`, `bool` to the rest of the system?**
  _156 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.07848944835246205 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.06278538812785388 - nodes in this community are weakly interconnected._