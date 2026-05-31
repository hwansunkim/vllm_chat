---
name: graphify
description: "코드·문서·PDF·이미지·동영상 등 다양한 파일을 쿼리 가능한 지식 그래프(Knowledge Graph)로 변환하는 도구. 코드베이스 구조 파악, 의존성 분석, 숨겨진 연결 관계 발굴, 'A와 B 사이 경로 찾기', 그래프로 시각화, graphify 빌드/쿼리/내보내기 요청 시 반드시 이 스킬을 사용. 설치: pip install graphifyy && graphify install."
---

# Graphify — 지식 그래프 빌더

파일 묶음을 읽어 **노드(개념·함수·파일) + 엣지(관계)** 로 이루어진 지식 그래프로 변환한다.
결과물은 인터랙티브 HTML 시각화, 쿼리 가능한 JSON 그래프, Obsidian vault, GRAPH_REPORT.md로 제공된다.

출처: https://github.com/safishamsi/graphify | PyPI: `graphifyy` (v0.1.14+)

## 설치

```bash
pip install graphifyy && graphify install   # Claude Code에 등록까지 한번에
```

선택적 의존성:
```bash
pip install graphifyy[pdf]    # PDF 추출
pip install graphifyy[video]  # 동영상/음성 전사 (faster-whisper)
pip install graphifyy[neo4j]  # Neo4j 내보내기
pip install graphifyy[all]    # 전체
```

## 핵심 커맨드

### 그래프 빌드
```bash
/graphify .                   # 현재 디렉토리 전체 분석
/graphify ./raw               # 특정 경로 분석
/graphify ./raw --mode deep   # 공격적 엣지 추출 (더 많은 INFERRED 관계)
/graphify . --update          # 변경된 파일만 재추출 (증분 업데이트)
/graphify ./raw --watch       # 파일 변경 시 자동 재빌드
```

### 쿼리 및 탐색
```bash
/graphify query "질문 내용"           # 의미 기반 검색 (기본 BFS, --dfs 옵션)
/graphify path "NodeA" "NodeB"        # 두 개념 사이 최단 경로
/graphify explain "NodeName"          # 특정 노드의 이웃 분석
/graphify add [URL]                   # 외부 논문·트윗·웹페이지 추가
```

### 내보내기 및 자동화
```bash
graphify export callflow-html         # 아키텍처 다이어그램 생성
graphify hook install                 # git 커밋 후 자동 재빌드 훅 설치
graphify prs                          # PR 대시보드 (그래프 영향 분석)
```

## 출력 파일 (graphify-out/ 디렉토리)

| 파일 | 설명 |
|------|------|
| `graph.html` | 인터랙티브 시각화 (노드 클릭·검색·커뮤니티 필터) |
| `graph.json` | 쿼리 가능한 영구 저장 그래프 |
| `GRAPH_REPORT.md` | 핵심 인사이트: god 노드, 의외의 연결, 추천 질문 |
| `obsidian/` | Obsidian Vault 형식 (위키링크 + canvas 레이아웃) |

## 지원 파일 형식

| 분류 | 형식 |
|------|------|
| 코드 (AST 기반) | Python, TypeScript, JS, Go, Rust, Java, C/C++, Ruby, C# 등 32개 언어 |
| 문서 | Markdown, HTML, YAML, DOCX, XLSX, TXT |
| PDF | 인용 및 개념 추출 포함 |
| 이미지 | PNG, JPG, WebP, GIF (Claude vision 활용) |
| 동영상/음성 | MP4, MP3, WAV (`[video]` 옵션 필요) |

## 신뢰도 태그

모든 엣지(관계)에 신뢰도 레벨 부여:
- `EXTRACTED` — 소스에서 명시적으로 확인된 관계
- `INFERRED` — 추론으로 도출된 관계
- `AMBIGUOUS` — 사람이 검토해야 하는 불확실한 관계

## vLLM Chat 프로젝트 활용 예시

```bash
# 전체 코드베이스를 그래프로 변환
/graphify . --update

# ABM 시뮬레이션 모듈과 backend 사이 연결 경로 탐색
/graphify path "ABM.simulation" "backend.api.simulation"

# 특정 모듈 의존 관계 분석
/graphify explain "backend.llm.client"

# 코드 변경 영향 범위 쿼리
/graphify query "어떤 모듈이 LLM 파이프라인에 의존하나?"
```

## 작동 원칙

- 코드·동영상은 **로컬에서만 처리** (tree-sitter, faster-whisper) — API 비용 없음
- 문서·이미지는 Claude API 호출 필요
- SHA256 캐시로 변경 없는 파일은 재처리 안 함
- `graphify-out/`을 git에 커밋해 팀과 그래프 공유 가능
- 텔레메트리 없음
