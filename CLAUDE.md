# vLLM Chat

@AGENTS.md
@AGENTS.local.md

## Claude Code의 역할

`AGENTS.md`의 역할 분담에 따라 Claude Code는 **개발**(설계·구현·리뷰 반영·커밋)을 맡는다. 전체 테스트 실행·실패 분석, 시뮬레이션 결과 분석, 변경 리뷰는 Codex에 맡긴다. 구현이 끝나면 `to_serve/`에 인수인계 메모를 쓰고 사용자에게 Codex 검증을 요청한다. 검증 중에는 수정한 부분과 직접 관련된 테스트만 돌린다.

## 하네스: vLLM Chat 개발

**목표:** FastAPI 백엔드 + Vanilla JS 프론트엔드 + ABM 시뮬레이션의 풀스택 개발을 에이전트 팀이 조율하며 수행.

**트리거:** 새 기능 추가, UI 수정, 백엔드 API 변경, ABM 시나리오 개발, 버그 수정, 코드 리뷰 등 vLLM Chat 관련 개발 작업 요청 시 `vllm-chat-dev` 스킬을 사용하라. 단순 질문이나 설명 요청은 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-05-26 | 초기 구성 | 전체 | - |
| 2026-05-26 | graphify 스킬 추가 | skills/graphify | 코드베이스 지식 그래프 빌더 등록 |
| 2026-10-20 | Codex 협업 체계 도입: `AGENTS.md` 공동 지침, `docs/decisions.md`, `to_*` 양식, QA를 Codex 인수인계로 전환 | 전체, skills/vllm-chat-dev | Claude 토큰을 개발에 집중하고 검증은 Codex가 수행 |
