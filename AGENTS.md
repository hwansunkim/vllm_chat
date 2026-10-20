# vLLM Chat / Rippletown — 에이전트 공동 지침

이 파일은 이 저장소에서 일하는 모든 코딩 에이전트(Claude Code, Codex)가 함께 따르는 규칙이다. Claude Code는 `CLAUDE.md`에서 이 파일을 불러온다. 규칙을 바꿀 때는 이 파일 하나만 고친다.

## 1. 프로젝트

- FastAPI 백엔드(`backend/`) + Vanilla JS 프론트엔드(`frontend/`) + LLM 다중 에이전트 시뮬레이션 엔진(`ABM/`).
- 시뮬레이션 엔진은 논문 "Rippletown"(Nature Human Behaviour 투고 목표)의 연구 도구다. 감염병 분석은 질병관리청 공저자가 담당한다.
- 엔진의 설계 원칙: **엔진이 세계(공간·시간·이동·감염)를 판정하고, LLM은 의도·발화·행동만 생성한다.** LLM은 감염 상태나 확률을 보지 않는다. 이 원칙을 깨는 변경은 결정 기록(`docs/decisions.md`) 없이 하지 않는다.

## 2. 역할 분담

| | Claude Code (개발) | Codex (검증) |
| --- | --- | --- |
| 주 업무 | 설계, 구현, 리뷰 반영, 커밋·머지 | 변경 리뷰, 전체 테스트 실행·실패 분석, 시뮬레이션 결과 분석, 회귀 테스트 제안 |
| 쓰는 곳 | 코드, `to_serve/` 인수인계 메모, `to_human/` | `to_master/` 검증 보고서, `to_human/` |
| 하지 않는 것 | 전체 테스트·대용량 로그의 반복 분석(Codex에 맡긴다) | **운영 코드 수정과 커밋.** 수정안은 보고서에 diff로 제안한다. |

예외: 사용자가 명시적으로 지시하면 Codex도 코드를 수정·커밋할 수 있다. 이때는 반드시 `docs/decisions.md`에 기록하고, 연구 가정을 바꾸는 변경이면 그 사실을 `to_master/` 보고서로 Claude에게 알린다.

## 3. 협업 루프

1. **구현 (Claude).** 작업 브랜치에서 구현·커밋하고 `to_serve/`에 인수인계 메모를 쓴다(`to_serve/_TEMPLATE_handoff.md`).
2. **검증 (Codex).** 메모와 `git diff master...<브랜치>`를 읽고, 테스트를 실행하고, 반박하는 관점으로 검토해 `to_master/`에 보고서를 쓴다(`to_master/_TEMPLATE_review.md`).
3. **반영 (Claude).** 보고서의 상위 지적을 **직접 확인한 뒤** 수정하고, 보고서 끝의 답변 절에 수용·반박·보류를 적는다. 보고서를 확인 없이 그대로 반영하지 않는다.
4. **종료.** Codex 판정이 통과이면 Claude가 머지한다.

- 반복은 **최대 2회**다. 그 뒤 남은 지적은 사용자에게 `to_human/`으로 넘긴다.
- 심각도 P3(표현·가독성)만 남으면 반복하지 않고 종료한다.
- 리뷰에 대한 리뷰를 다시 리뷰하는 연쇄는 하지 않는다. 의견이 갈리면 근거를 정리해 `to_human/`으로 넘긴다.

## 4. 폴더 규약

| 폴더 | 쓰는 쪽 → 읽는 쪽 | 내용 |
| --- | --- | --- |
| `to_serve/` | Claude → Codex | 인수인계 메모, 검증을 요청하는 문서 |
| `to_master/` | Codex → Claude | 검증·리뷰 보고서 |
| `to_human/` | 누구든 → 사용자 | 사람의 결정이 필요한 사항, 최종 정리 |

- 파일 이름: `<YYYY-MM-DD>_<작업명>_<종류>.md`. 종류는 `handoff`, `review`, `decision`, `summary` 중 하나다(예: `2026-10-21_lone-hop-idle_review.md`).
- 모든 문서 머리에 다음을 적는다: 작성 에이전트, 작성일, **기준 커밋**(검토·작업한 HEAD), 대상 파일·브랜치. 실행 결과를 인용하면 실행 ID와 설정 파일 경로도 적는다.
- `_TEMPLATE_`로 시작하는 파일은 양식이다. 지우지 않는다.

## 5. 결정 기록

- 작업을 시작하기 전에 `docs/decisions.md`를 읽는다.
- 연구 가정(모델 공식·단위·기본값, 실험 범위, 시나리오 규칙)을 바꾸면 같은 커밋에서 한 줄을 추가한다.
- 결정과 충돌하는 지시를 받으면 작업하기 전에 사용자에게 확인한다.

## 6. 환경 (Windows, conda)

기본 `python`(base miniconda)은 이 프로젝트의 어느 용도에도 맞지 않는다. 항상 아래 실행 파일을 쓴다.

| 용도 | 실행 파일 |
| --- | --- |
| 테스트 | `C:\Users\ADMIN\miniconda3\envs\vllm\python.exe -m pytest tests/ -q` |
| 웹 서버 실행 | `C:\Users\ADMIN\miniconda3\envs\fastapi\python.exe run.py` (포트 8888). `requirements.txt`와 어긋날 수 있으니 의존성은 이 환경에서 확인한다. |
| LaTeX 컴파일 | `C:\Users\ADMIN\miniconda3\envs\tex\Library\bin\tectonic.exe -X compile <파일>.tex --outdir <임시 폴더>` |

- 전체 테스트는 Windows에서 모두 통과하는 상태다(2026-10-14 기준 874개). 새 실패는 기존 실패로 넘기지 말고 원인을 보고한다.
- 엔진 코드를 바꾼 뒤 실제 시뮬레이션에 반영하려면 웹 서버를 재시작해야 한다.
- 시뮬레이션 기록: `logs_graph/simulation.db`(실행·로그·이벤트), `memory.db`(저장된 시나리오). 분석할 때는 읽기 전용으로 연다.

## 7. 언어

- **산출물은 영어:** 논문 원고, 그림 안의 글, 영문 문서.
- **보고와 지시 문서는 한국어:** 사용자에게 보내는 보고, `to_*` 폴더의 메모·보고서, 이 파일.
- 코드 주석은 주변 코드의 언어를 따른다(엔진 코드는 대부분 한국어).

## 8. 논문 문서

- Methods 최종본: `docs/Rippletown — Methods (draft).md`(영문), `docs/Rippletown — Methods (draft, ko).md`(한국어), LaTeX 조각 `docs/latex/methods.tex`.
- Methods를 검토·수정할 때는 `docs/methods-review-guidelines.md`를 따른다. 현재 쓰는 기능을 긍정문으로 서술하고, 과거 동작이나 장애 대비 처리는 방법론으로 쓰지 않는다.
- LaTeX를 수정하면 컴파일해 확인한 뒤 넘긴다.

## 9. Git

- 기본 브랜치는 `master`다. 개발은 작업 브랜치에서 하고, 검증이 끝나면 fast-forward로 머지한다.
- 커밋 메시지는 기존 형식(`fix:`, `feat:`, `test:`, `refactor:` + 한국어 요약)을 따른다.
- 푸시는 사용자가 요청할 때만 한다.
