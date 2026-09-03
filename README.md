# vLLM Web Chat

로컬 vLLM 서버와 연동하는 멀티 대화 웹 채팅 클라이언트.  
RAG 기반 메모리 시스템으로 컨텍스트 윈도우를 효율적으로 관리한다.

## 실행

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:8888
```

또는 uvicorn을 직접 실행할 수 있다.

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8888 --reload
```

vLLM 서버 주소와 모델은 웹 UI의 서버 관리 화면 또는 `/api/servers` API로 등록한다.
초기 서버 목록은 `servers.json` 파일이 있을 때 첫 실행 시 DB에 시드된다.

## 헤드리스 시뮬레이션 CLI

GUI 없이 ABM 시나리오를 돌리고 스크린플레이 마크다운을 얻는다. 같은 시나리오를 N회
반복하거나, 파라미터를 바꿔가며 스윕하거나, 회귀 평가에 쓴다.

> 모든 명령은 **리포지토리 루트에서** 실행한다 (`servers.json`, `logs_graph/`,
> `memory.db` 가 상대 경로).

### `run` — 시나리오 실행 → 마크다운

```bash
# 1회 실행 → 파일 (기본 파일명: {시나리오명}_{YYYY-MM-DD_HHMM}.md)
python -m ABM.cli run scenario.json
python -m ABM.cli run scenario.json -o out.md
python -m ABM.cli run scenario.json -o -                 # stdout

# 설정만 검증 + 엔진 프롬프트 계약 미리보기 (LLM 서버 불필요)
python -m ABM.cli run scenario.json --dry-run

# config 오버라이드
python -m ABM.cli run scenario.json \
    --max-waves 200 --target-minutes 4320 \
    --server-id <uuid> --temperature 0.9 --step-delay 0 --seed 42

# 배치 — run 별로 로그 디렉토리와 run_id 를 격리, DB 에 각각 기록
python -m ABM.cli run scenario.json -n 20 --outdir results/ --concurrency 4

# DB 에 저장된 시나리오를 id 로
python -m ABM.cli run --scenario-id <uuid> -n 5 --outdir results/ --no-db

# 회귀 평가 — 침묵/에이전트 소진으로 끝나면 종료 코드 1
python -m ABM.cli run scenario.json --strict --quiet -o -

# 마크다운 섹션 토글
python -m ABM.cli run scenario.json --include time,action,move
python -m ABM.cli run scenario.json --exclude infection,meeting
```

| 옵션 | 설명 |
|---|---|
| `scenario` | 시나리오 JSON 경로 (위치 인자) |
| `--scenario-id <uuid>` | 파일 대신 DB(`simulation_scenarios`)에서 로드 |
| `-o, --output` | 출력 파일 또는 `-`(stdout). 기본은 자동 파일명 |
| `--outdir` | 출력 디렉토리 (`-n > 1` 이면 필수), 파일은 `{이름}_run{i}_{tag}.md` |
| `-n, --runs` | 반복 횟수 (기본 1) |
| `--concurrency` | 동시 실행 수 (기본 1). LLM I/O 바운드라 배치에서 이득이 큼 |
| `--include a,b,c` / `--exclude a,b,c` | 마크다운 섹션. 키: `time action move appearance world intervention infection meeting summary` (기본: `summary` 제외 전부) |
| `--max-waves` / `--target-minutes` | config 오버라이드 (`--target-minutes 0` = 미사용) |
| `--server-id` / `--temperature` | LLM 오버라이드 |
| `--step-delay` | wave 간 대기(초). 배치는 `0` 권장 |
| `--seed` | Python `random` 시드 (N회 실행은 `seed+i`). LLM 응답은 여전히 비결정적 |
| `--no-db` | `logs_graph/simulation.db` 에 기록하지 않음 (기본은 기록 → GUI run 히스토리에 노출) |
| `--dry-run` | 설정 검증 + 엔진 계약 출력만, 실행 안 함 |
| `--quiet` | stderr 진행률 억제 |
| `--strict` | `end_reason ∈ {silence, no_agents}` 이면 종료 코드 1 |

**종료 코드**: `0` 정상 · `1` `--strict` 조건 · `2` 설정/IO 오류 · `3` 실행 예외

**진행률(stderr)**: `[run 3/20] wave 45/200 · 턴 118 · a, b` →
마지막에 `완료 18 / 오류 2 — max_waves 16 / silence 2 · 평균 wave 87.3 · 평균 턴 241.0`

### `export` — 이미 끝난 실행을 다시 마크다운으로

이벤트와 대화는 `simulation.db` 에 영속화되므로, GUI 로 돌린 실행도 다른 토글로 다시
뽑을 수 있다.

```bash
python -m ABM.cli export --run-id 5a6771f6-0ec5-47c1-b7b5-3129164d05b4 -o out.md
python -m ABM.cli export --scenario "4인가족(엄마아빠누나동생)_v3" --latest
python -m ABM.cli export --run-id <uuid> --include summary        # 요약 포함
```

run_id 찾기:

```bash
sqlite3 logs_graph/simulation.db \
  "SELECT run_id, scenario_name, started_at FROM simulation_runs ORDER BY started_at DESC LIMIT 10"
```

### 시나리오 JSON 형식

`run` 은 세 가지 모양을 모두 받는다:

- `SimStartConfig` 자체 (`{"agents": [...], "background": "...", ...}`)
- `{"name": "...", "config": { ... }}`
- `{"name": "...", "config_json": "..."}` — `/api/simulation/scenarios` 응답 항목

필드 정의는 `backend/api/simulation/schemas.py` 의 `SimStartConfig`, 최소 예시는
`tests/fixtures/golden_scenario.json` 참고. GUI 설정 화면에서 시나리오를 저장한 뒤
그 JSON 을 내보내는 것이 가장 쉽다.

### 주의

- **배치 결정론**: `--seed` 는 전역 `random` 시드라 `--concurrency > 1` 에서는 run 별
  격리가 안 된다 (경고 출력). 완전한 재현이 필요하면 `--concurrency 1`.
- CLI 로 돌린 run 도 스냅샷을 남기므로 GUI 의 `/load`·`/resume`·인터뷰에서 그대로 열린다.
- 마크다운 포매터는 프론트엔드(`frontend/js/sim/export/markdown.js`)와 Python
  (`ABM/export/markdown.py`) 두 벌이며 출력이 바이트 단위로 같도록 골든 테스트로
  고정돼 있다. 포맷을 바꾸면 양쪽을 함께 고쳐야 한다.

## 문서

- [아키텍처](docs/architecture.md) — 전체 시스템 구조
- [메모리 시스템](docs/memory-system.md) — RAG 메모리 동작 원리
- [API 레퍼런스](docs/api.md) — REST API 명세
- [Vector DB 전환 검토](docs/vectordb.md) — SQLite → Vector DB 마이그레이션 고려사항
