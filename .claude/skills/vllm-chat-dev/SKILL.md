---
name: vllm-chat-dev
description: "vLLM Chat 프로젝트 개발 오케스트레이터. 새 기능 추가, UI 개선, API 수정, ABM 시나리오 개발, 버그 수정, 코드 리뷰 요청 시 반드시 이 스킬을 사용. 후속 작업: 다시, 재실행, 수정, 보완, 개선, 업데이트, 이전 결과 기반 작업 요청 시에도 반드시 사용. 채팅 UI, 백엔드 API, 메모리 시스템, 시뮬레이션, 에이전트 등 vLLM Chat 관련 모든 개발 작업에 사용."
---

# vLLM Chat Development Orchestrator

vLLM Chat 프로젝트(FastAPI 백엔드 + Vanilla JS 프론트엔드 + ABM 시뮬레이션)의 개발 작업을 조율하는 오케스트레이터.

## 실행 모드: 하이브리드

| Phase | 모드 | 이유 |
|-------|------|------|
| 풀스택 피처 개발 | 에이전트 팀 | backend-dev ↔ frontend-dev 간 API 계약 실시간 조율 필요 |
| ABM 독립 작업 | 서브 에이전트 | abm-engineer 단독, 팀 통신 불필요 |
| QA/버그 검증 | 서브 에이전트 | qa-reviewer 단독, 독립 검증이 핵심 |

## 에이전트 구성

| 에이전트 | 타입 | 역할 | 스킬 |
|---------|------|------|------|
| backend-dev | 커스텀 | FastAPI, SQLite, LLM 파이프라인 | backend-skill |
| frontend-dev | 커스텀 | HTML, CSS, JavaScript, UI | frontend-skill |
| abm-engineer | 커스텀 | ABM 시나리오, 시뮬레이션 로직 | abm-skill |
| qa-reviewer | 커스텀 | 코드 리뷰, 버그 탐지, 정합성 검증 | — |

## 워크플로우

### Phase 0: 컨텍스트 확인 (후속 작업 지원)

1. `_workspace/` 존재 여부 확인
2. 실행 모드 결정:
   - **`_workspace/` 미존재** → 초기 실행. Phase 1로 진행
   - **`_workspace/` 존재 + 부분 수정 요청** → 부분 재실행: 해당 에이전트만 재호출, 기존 산출물 유지
   - **`_workspace/` 존재 + 새 작업** → 새 실행: `_workspace/`를 `_workspace_{YYYYMMDD_HHMMSS}/`로 이동 후 Phase 1 진행

### Phase 1: 작업 분류 및 준비

1. 사용자 요청 분석:
   - **풀스택 피처**: 백엔드 API + 프론트엔드 UI 모두 변경 필요
   - **백엔드 단독**: API 엔드포인트, DB, LLM 파이프라인만 변경
   - **프론트엔드 단독**: HTML/CSS/JS만 변경
   - **ABM 작업**: 시나리오 추가/수정, 시뮬레이션 로직 변경
   - **QA/버그**: 코드 리뷰, 버그 탐지, 테스트 실행
2. 작업 디렉토리 생성: `_workspace/` (또는 이동 후 재생성)
3. 작업 범위 및 API 계약(계획) 정리

### Phase 2: 실행 (작업 유형별 분기)

#### 2-A. 풀스택 피처 개발 (에이전트 팀)
**실행 모드: 에이전트 팀**

1. 팀 구성:
   ```
   TeamCreate(
     team_name: "vllm-chat-feature-team",
     members: [
       {
         name: "backend-dev",
         agent_type: "backend-dev",
         model: "opus",
         prompt: "당신은 vLLM Chat 백엔드 개발자입니다. backend-skill 스킬을 참조하여 작업하세요. 작업: {상세 작업 내용}. API 스펙 완성 후 frontend-dev에게 SendMessage로 엔드포인트 URL, method, request/response shape을 전달하세요. 완료 시 _workspace/backend_done.md 생성 후 리더에게 보고."
       },
       {
         name: "frontend-dev",
         agent_type: "frontend-dev",
         model: "opus",
         prompt: "당신은 vLLM Chat 프론트엔드 개발자입니다. frontend-skill 스킬을 참조하여 작업하세요. 작업: {상세 작업 내용}. backend-dev로부터 API 스펙을 수신한 후 UI를 구현하세요. 완료 시 _workspace/frontend_done.md 생성 후 리더에게 보고."
       }
     ]
   )
   ```

2. 작업 등록:
   ```
   TaskCreate(tasks: [
     {title: "백엔드 API 구현", description: "{상세}", assignee: "backend-dev"},
     {title: "프론트엔드 UI 구현", description: "{상세}", assignee: "frontend-dev", depends_on: ["백엔드 API 구현"]}
   ])
   ```

3. 팀원 자체 조율:
   - backend-dev: API 구현 후 스펙을 SendMessage로 frontend-dev에게 전달
   - frontend-dev: 스펙 수신 후 UI 구현
   - 리더: TaskGet으로 진행 상황 모니터링

4. 완료 후 Phase 3 (QA) 진행, 팀 정리

#### 2-B. 백엔드/프론트엔드 단독 작업 (서브 에이전트)
**실행 모드: 서브 에이전트**

```
Agent(
  subagent_type: "backend-dev" 또는 "frontend-dev",
  model: "opus",
  prompt: "backend-skill (또는 frontend-skill) 스킬을 참조하여 작업하세요. 작업: {상세}. 완료 시 _workspace/done.md에 변경 파일 목록과 요약을 기록하세요."
)
```

#### 2-C. ABM 작업 (서브 에이전트)
**실행 모드: 서브 에이전트**

```
Agent(
  subagent_type: "abm-engineer",
  model: "opus",
  prompt: "abm-skill 스킬을 참조하여 작업하세요. 작업: {시나리오 명세 또는 로직 변경 내용}. 완료 시 _workspace/abm_done.md에 생성/수정한 파일 목록을 기록하세요."
)
```

#### 2-D. QA/버그 조사 (서브 에이전트)
**실행 모드: 서브 에이전트**

```
Agent(
  subagent_type: "qa-reviewer",
  model: "opus",
  prompt: "검증 대상: {변경 파일 목록 또는 버그 증상}. API-프론트엔드 경계면 교차 검증, 테스트 실행(python -m pytest tests/ -v), 코드 리뷰를 수행하세요. 결과를 _workspace/qa_report.md에 파일:라인 형식으로 정리하세요."
)
```

### Phase 3: QA 검증 (풀스택 피처 완료 후)

풀스택 피처 개발(2-A) 완료 후 자동으로 QA 실행:

1. 에이전트 팀 정리 (TeamDelete)
2. qa-reviewer 서브 에이전트 실행 (2-D 패턴)
3. QA 보고서(`_workspace/qa_report.md`) 확인
4. 이슈 발견 시: 해당 에이전트에게 수정 요청 (2-B 패턴으로 단독 재실행)

### Phase 4: 정리 및 보고

1. `_workspace/` 보존 (삭제 금지 — 감사 추적용)
2. 사용자에게 요약 보고:
   - 변경된 파일 목록
   - 주요 변경 내용
   - QA 결과 (발견 이슈 및 처리 여부)
3. 후속 작업 피드백 요청 ("개선할 부분이 있나요?")

## 데이터 흐름

```
[오케스트레이터]
    │
    ├── 풀스택 피처 → TeamCreate → [backend-dev] ←SendMessage→ [frontend-dev]
    │                                    │                          │
    │                             _workspace/backend_*     _workspace/frontend_*
    │                                    └──────────────────────────┘
    │                                              ↓
    │                                    TeamDelete → qa-reviewer
    │                                              ↓
    │                                    _workspace/qa_report.md
    │
    ├── 단독/ABM → Agent() → _workspace/done.md
    │
    └── [최종 보고]
```

## 에러 핸들링

| 상황 | 전략 |
|------|------|
| 팀원 1명 실패 | SendMessage로 상태 확인 → 재시작 또는 단독 재실행(2-B) |
| QA에서 버그 발견 | 해당 에이전트 단독 재실행 후 재검증 |
| API 계약 불일치 | backend-dev와 frontend-dev 간 SendMessage로 스펙 재합의 |
| 테스트 실패 | qa_report.md 기반으로 수정 요청 |

## 테스트 시나리오

### 정상 흐름 (풀스택 피처)
1. "채팅 메시지에 복사 버튼 추가해줘" 요청
2. Phase 1: 풀스택 피처로 분류
3. Phase 2-A: backend-dev (복사 API 불필요 — 순수 프론트엔드로 재분류) → 2-B로 전환
4. frontend-dev: `messages.js`에 복사 버튼 DOM 추가, CSS 스타일 추가
5. Phase 3: qa-reviewer — XSS 체크, clipboard API 브라우저 호환성 확인
6. Phase 4: 변경 파일 목록 보고

### 에러 흐름 (ABM 시나리오 추가)
1. "병원 대기실 시나리오 추가해줘" 요청
2. Phase 2-C: abm-engineer 서브 에이전트 실행
3. abm-engineer가 import 에러로 실패
4. 오케스트레이터: `scenarios/__init__.py` 등록 누락 확인
5. abm-engineer 재실행 (수정 지시 포함)
6. 정상 완료, qa_report 없이 Phase 4 진행
