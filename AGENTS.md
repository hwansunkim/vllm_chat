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

1. **구현 (Claude).** 작업 브랜치에서 구현·커밋하고 `to_serve/`에 인수인계 메모를 쓴다(`to_serve/_TEMPLATE_handoff.md`). 메모에서 "유지된다", "바뀌지 않는다"고 적은 동작은 **테스트로 뒷받침**하고, 하위 에이전트가 쓴 메모라면 넘기기 전에 Claude가 그 주장을 직접 확인한다.
2. **검증 (Codex).** 메모와 `git diff master...<브랜치>`를 읽고, 테스트를 실행하고, 반박하는 관점으로 검토해 `to_master/`에 보고서를 쓴다(`to_master/_TEMPLATE_review.md`).
3. **반영 (Claude).** 보고서의 상위 지적을 **직접 확인한 뒤** 수정하고, 보고서 끝의 답변 절에 수용·반박·보류를 적는다. 보고서를 확인 없이 그대로 반영하지 않는다.
4. **종료.** Codex 판정이 통과이면 Claude가 머지하고, 그 작업의 인수인계 메모와 검증 보고서를 커밋해 이력으로 남긴다.

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
- 세 폴더의 문서는 감사 기록으로 **커밋한다.** 검증 루프의 메모·보고서는 루프가 끝날 때 함께 커밋한다.

## 5. 결정 기록

- 작업을 시작하기 전에 `docs/decisions.md`를 읽는다.
- 연구 가정(모델 공식·단위·기본값, 실험 범위, 시나리오 규칙)을 바꾸면 같은 커밋에서 한 줄을 추가한다.
- 결정과 충돌하는 지시를 받으면 작업하기 전에 사용자에게 확인한다.

## 6. 환경

이 파일에는 **어느 컴퓨터에서나 같은 규칙**만 적는다. 실행 파일 경로, conda 설치 위치, Codex CLI 경로, LLM 서버 주소처럼 컴퓨터마다 다른 값은 저장소 루트의 **`AGENTS.local.md`**(git 추적 제외)에 둔다. 작업을 시작할 때 이 파일도 읽는다. 없으면 `AGENTS.local.example.md`를 복사해 만들고 사용자에게 값을 확인한다(10절).

| 용도 | conda 환경 이름 | 명령 (경로는 `AGENTS.local.md`의 값을 쓴다) |
| --- | --- | --- |
| 테스트 | `vllm` | `<vllm python> -m pytest tests/ -q -p no:cacheprovider` |
| 웹 서버 실행 | `fastapi` | `<fastapi python> run.py` (포트 8888) |
| LaTeX 컴파일 | `tex` (Tectonic) | `<tectonic> -X compile <파일>.tex --outdir <임시 폴더>` |

- 기본 `python`(conda base 등)은 이 프로젝트의 어느 용도에도 맞지 않는다. 위 환경의 인터프리터를 쓴다.
- `-p no:cacheprovider`: 캐시 플러그인을 끄면 Codex 샌드박스에서 `.pytest_cache` 쓰기 권한 경고가 나지 않는다.
- `fastapi` 환경은 `requirements.txt`와 어긋날 수 있으니 의존성은 그 환경에서 확인한다.
- 전체 테스트는 모두 통과하는 상태를 유지한다. 새 실패는 기존 실패로 넘기지 말고 원인을 보고한다.
- 엔진 코드를 바꾼 뒤 실제 시뮬레이션에 반영하려면 웹 서버를 재시작해야 한다.
- **각 컴퓨터가 따로 가지는 것(git에 넣지 않는다):** 시뮬레이션 기록 `logs_graph/simulation.db`·`memory.db`(분석할 때는 읽기 전용으로 연다), 실행용 시나리오 폴더 `run/`, LLM 서버 주소(환경 변수 `VLLM_BASE_URL` 등), `AGENTS.local.md`.

## 7. 언어

- **산출물은 영어:** 논문 원고, 그림 안의 글, 영문 문서.
- **에이전트 사이의 문서는 영어도 된다:** `to_serve/` 인수인계 메모와 `to_master/` 검증·분석 보고서는 영어로 써도 된다(한국어도 무방).
- **사용자에게 가는 최종 보고는 한국어:** 대화창의 최종 보고와 `to_human/` 문서. 이 파일도 한국어로 유지한다.
- 코드 주석은 주변 코드의 언어를 따른다(엔진 코드는 대부분 한국어).

## 8. 논문 문서

- Methods 최종본: `docs/Rippletown — Methods (draft).md`(영문), `docs/Rippletown — Methods (draft, ko).md`(한국어), LaTeX 조각 `docs/latex/methods.tex`.
- Methods를 검토·수정할 때는 `docs/methods-review-guidelines.md`를 따른다. 현재 쓰는 기능을 긍정문으로 서술하고, 과거 동작이나 장애 대비 처리는 방법론으로 쓰지 않는다.
- LaTeX를 수정하면 컴파일해 확인한 뒤 넘긴다.

## 9. Git

- 기본 브랜치는 `master`다. 개발은 작업 브랜치에서 하고, 검증이 끝나면 fast-forward로 머지한다.
- 커밋 메시지는 기존 형식(`fix:`, `feat:`, `test:`, `refactor:` + 한국어 요약)을 따른다.
- 푸시는 사용자가 요청할 때만 한다.

## 10. Codex 자동 실행과 새 환경 준비

### Codex 자동 실행 (Claude가 수행)
사용자가 수동 전달을 원하지 않으면 Claude가 Codex CLI를 직접 실행한다. 명령 형식:

```
<codex> exec -s workspace-write -C <저장소 루트> -o <임시 폴더>/<작업명>_last.md "<지시문>" > <임시 폴더>/<작업명>_log.txt 2>&1
```

- `<codex>`의 실제 경로는 `AGENTS.local.md`에 있다. 임시 폴더는 저장소 밖(세션 임시 폴더)을 쓴다.
- 오래 걸리므로 백그라운드로 실행하고 완료 알림을 기다린다. 진행 로그는 읽지 않고 `-o`로 받은 마지막 요약과 보고서의 필요한 절만 읽는다.
- 지시문에는 반드시 넣는다: `AGENTS.md`를 따를 것, 읽을 `to_serve/` 메모, 쓸 `to_master/` 보고서 경로, 운영 코드·DB 수정과 커밋 금지, 마지막 응답은 판정 3줄 요약.
- 실행이 끝나면 `git status`로 운영 코드가 바뀌지 않았는지 확인한다.
- 2차 검증 지시문: 같은 보고서에 "2차 검증 결과" 절과 최신 판정을 덧붙이고, 보류 항목은 사유의 타당성만 판단하게 한다.

### 새 컴퓨터에서 이 협업 체계를 쓰기 위한 준비
1. Claude Code와 Codex CLI(`npm i -g @openai/codex`)를 설치하고 `codex login`으로 ChatGPT 계정에 로그인한다.
2. conda 환경 `vllm`(테스트, `pytest` 포함), `fastapi`(서버), 필요하면 `tex`(`conda install -c conda-forge tectonic`)를 만든다.
3. `AGENTS.local.example.md`를 `AGENTS.local.md`로 복사하고 그 컴퓨터의 경로와 LLM 서버 주소를 채운다.
4. 테스트 명령으로 전체 스위트가 통과하는지 확인한다.
