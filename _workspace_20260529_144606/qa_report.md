# 코드 리뷰 & 모듈화 계획

대상 코드베이스: `/home/wskim/work/vllm_chat` (master 브랜치, 2026-05-28 시점)
리뷰 범위: ABM 시뮬레이션 코어 + 백엔드 시뮬레이션 API + 프론트엔드 시뮬레이션 모듈

---

## 1. 파일별 리뷰 요약

### 1.1 `backend/api/simulation.py` (567줄)

**A. 책임 분리 (SRP) — 위반**
- 한 파일이 다음 6개 책임을 모두 수행:
  1. Pydantic 스키마 정의 (L45–L92)
  2. 글로벌 시뮬레이션 상태 관리 (L28–L40, `_sim` dict)
  3. SSE 스트림 제너레이터 (L292–L315)
  4. 시뮬레이션 실행 스레드 부트스트랩 (L107–L206 `start`, L218–L280 `continue`, L369–L480 `resume`)
  5. Run/Scenario CRUD (L348–L367, L518–L567)
  6. Agent 상태 조회(`/context`, `/memory`) (L318–L343, L489–L497)

**B. 중복 코드**
- `start_simulation._run` (L123–L201), `continue_simulation._run` (L230–L275), `resume_simulation._run` (L400–L475) — 세 함수가 사실상 동일한 라이프사이클을 가지며 차이는 "에이전트를 새로 만드냐 / 기존 sim_obj 재사용이냐 / DB 스냅샷에서 복원하냐"뿐.
  - `db.create_run` → `sim.run(...)` → `_sim["status"]=...` → `db.finish_run` → `eq.put(None)` 패턴이 3회 반복.
  - try/except/finally의 에러 처리 코드도 거의 동일 (L193–L201, L266–L275, L466–L475).
- `alias_map = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}` — L161과 L415에서 중복.
- "시간 포맷팅" 코드가 프론트의 `refreshRunHistory` / `openAllRunsModal` / `openRunReplay`에서 3회 반복되는 것과 짝을 이룸 (백엔드는 `time.time()` 그대로 내려보냄).
- 시나리오 CRUD의 DB 연결 패턴 `conn = get_db() / ... / conn.close()`이 4개 핸들러에서 반복 (L520, L534, L550, L564) — 컨텍스트 매니저 부재.

**C. 파일/함수 크기**
- 파일 567줄 — 분할 임계 초과.
- `start_simulation._run` 80줄(L123–L201), `resume_simulation._run` 75줄(L400–L475) — 모두 50줄 초과, 내부에서 import도 수행 (L125–L128, L404–L408).

**D. 결합도**
- 함수 내부에서 `from ABM.agent import Agent` 등을 늦게 import (L125, L404). 순환 의존성을 피하려는 의도지만, 모듈 경계가 모호함을 시사.
- 모듈 전역 `_sim` dict가 모든 핸들러의 암묵적 공유 상태 — 테스트 격리 불가.

**E. 네이밍 일관성**
- `start_agent` (snake_case, Python) vs `start_agent` (camelCase 없음 — 일관됨, 다만 `currentScenarioId` 등 프론트와 페이로드 키 매핑이 손으로 풀려있어 휴먼 에러 여지 있음).
- HTTPException 메시지가 영어/한국어 혼용: "Simulation already running" (L110), "이어서 실행은 완료 또는 중지된 시뮬레이션에서만 가능합니다" (L221) — 사용자 노출 문자열은 한 언어로 통일 필요.

**F. 버그 가능성 (실제 발견)**

| # | 위치 | 증상 |
|---|------|------|
| B1 | L107–L122 `start_simulation` | `_sim["status"] == "running"` 체크와 `_sim.update(status="running", ...)` 사이에 락이 없음. 두 클라이언트가 동시에 POST하면 두 워커 스레드가 모두 시작되어 `_sim["thread"]` 가 덮어쓰여 진다. **Race condition.** 수정 제안: 모듈 전역 `_sim_lock = threading.Lock()`을 두고 체크–세팅을 원자적으로. |
| B2 | L292–L315 `stream_events` | `q = _sim.get("event_queue")`를 핸들러 등록 직후에만 읽는다. 만약 SSE 클라이언트가 `/start` 응답 직후 바로 `/stream`으로 붙기 전에 시뮬레이션이 끝나면 sentinel `None`이 큐에서 이미 소비된 상태일 수 있다. 또한 다음 `/start` 호출이 새 큐로 `_sim["event_queue"]`를 갈아끼우면, 이전 SSE 제너레이터는 새 큐를 읽게 되어 이벤트가 섞임. **Cross-run event leakage.** |
| B3 | L218–L228 `continue_simulation` | `_sim["sim_obj"]`를 직접 변형 (`sim_obj._event_queue = eq` 등). `Simulation` 인스턴스가 다른 스레드에서 여전히 메모리를 정리 중일 수도 있는데, 동기화 없음. 또한 `sim_obj.completed_waves = 0` (L241)으로 리셋하지만 DB의 `total_waves`는 별도 finish_run에서 갱신 — `db.create_run` 이전 run을 새 run_id로 분리하므로 OK이지만 의도가 코드만으로는 불명확. |
| B4 | L189–L199 (start의 except/finally) | 예외 경로에서 `db.finish_run(run_sim_id, "error", 0, 0)` 호출 시 `db` / `run_sim_id`가 `try` 블록 안의 지역 변수라 **try의 어느 라인에서 예외가 나느냐에 따라 NameError**가 발생 가능. 예: `cfg.model_dump_json()` 직전에서 cfg 검증이 실패하면 `db`가 미정의. `continue` (L231–L233)와 `resume` (L401–L402)은 미리 None 초기화로 방어하나 `start`는 누락. |
| B5 | L97–L102 `_blocking_get` | 30초 타임아웃 후 `{"type":"ping","data":{}}`을 반환. 클라이언트가 `ping` 이벤트를 잘 처리(L498에서 noop)하지만, 시뮬레이션이 끝났는데 sentinel이 사라진 경우 ping만 계속 흘러나옴 → 클라이언트가 `/stream`에서 영영 빠져나오지 못함. 별개로, 시뮬레이션 종료 후 새 `/start` 가 없는 상태에서 `/stream`이 호출되면 큐의 sentinel `None`은 단 한 번만 소비되므로, 두 번째 SSE 접속자가 큐를 들고 영원히 대기. |
| B6 | L188 vs L260 vs L460 | 상태 결정 로직(`"stopped" if stop_ev.is_set() else "done"`)이 3회 중복 — 동기/조건 변경 시 한 곳만 수정되면 분기마다 상태가 어긋날 위험. |
| B7 | L233 `db = sim_obj._db` | private 속성 접근. `Simulation`이 `db` 프로퍼티를 노출하지 않아 호출부가 캡슐화를 깬다. |
| B8 | L432–L433 vs L433 | `init_agents = list(saved_active) if saved_active is not None else None` (L434)인데, 이는 set → list 변환 — 원소 순서가 불정. 시뮬레이션 결과 재현성에는 큰 영향은 없지만, `initial_agents` 인자가 set로 들어가도 되도록 `Simulation.__init__` 시 처리. |
| B9 | L455 (resume) | `events=[e.model_dump() for e in cfg.events]` — resume 시 원래 시나리오의 이벤트가 다시 실행됨. wave가 0부터 시작하므로 이미 발생한 이벤트가 재실행될 수 있다. **Event replay bug 가능성.** |
| B10 | L385 `cfg = SimStartConfig(**cfg_dict)` | resume 시 `extra_fields`가 cfg_dict에 없으면 기본값(emotion, action)이 적용되지만, 원본 실행에서는 다른 필드를 썼을 수 있음. 저장 스냅샷이 `extra_fields`를 항상 포함하는지 보장 필요. |

---

### 1.2 `ABM/simulation.py` (460줄)

**A. SRP — 위반**
- `Simulation` 클래스가 다음을 모두 담당:
  1. 에이전트 컨테이너 / 활성 집합 관리 (L36–L62)
  2. 파일 I/O (shared_log.json, edges.json) (L75–L85)
  3. SSE 이벤트 발행 (`_emit`) (L71–L73)
  4. 시나리오 이벤트 실행 (`_execute_event`) (L124–L190)
  5. 메모리 압축 트리거 (L196–L217)
  6. 에이전트 한 스텝 실행 (`_step_agent`) (L223–L365, **143줄**)
  7. Wave-based BFS 메인 루프 (`run`) (L371–L460, **90줄**)
  8. Display name ↔ key 매핑 (L48–L51, `_normalize_target`)
  9. DB 로깅 (`db.log_turn`, `db.save_agent_snapshots`)

**B. 중복**
- L286–L288, L293–L295의 "incoming pop" 폴백 로직이 중복 (실패 시 큐에 push했던 user 메시지 되돌리기).
- 이벤트 발행 시 dict 생성 패턴(`self._emit("turn_start", {...})`, L267 / L284 / L292 / L336 등)이 각 분기마다 손으로 작성됨.

**C. 함수 크기**
- `_step_agent` 143줄 (L223–L365) — 단일 책임 위반의 정수. 내부에 다음 5단계가 직렬화:
  1. incoming 메시지를 메모리에 추가 (L236–L247)
  2. 압축 트리거 판정 (L251–L261)
  3. 트림 + 토큰 추정 + `turn_start` emit (L263–L274)
  4. LLM 호출 + 에러 핸들링 (L276–L296)
  5. 응답 파싱 + 메모리/로그/엣지 갱신 + `turn_complete` emit + DB log_turn (L298–L357)
- `run` 90줄 — wave 루프 한 함수에 이벤트 실행/active 관리/병렬 실행/next_wave 구성이 모두 들어감.

**D. 결합도**
- `_step_agent`가 `parse_json_response` (L302), `chat_response` (L279), `db.log_turn` (L351), `_emit`까지 직접 호출 — LLM, 파싱, 영속성, 이벤트 버스가 모두 한 메서드에서 결합.
- `_compress_agent` 안에서 `from .memory_compressor import compress`를 함수 내 import (L198) — 순환 의존성 회피 흔적.

**E. 네이밍**
- `_pending_wave` (dict로 "다음 wave 후보") vs `current_wave` (실행 중 wave) — 의미가 비대칭. `_unprocessed_wave_queue` 같은 이름이 더 명확.
- L260 `est / active_agent._token_limit >= _COMPRESSION_THRESHOLD` — `_token_limit`은 private 속성 접근. Agent가 public property 미제공.

**F. 버그 가능성**

| # | 위치 | 증상 |
|---|------|------|
| S1 | L72–L73 `_emit` | `event_queue.put()`이 무한 대기 큐이지만, 만약 SSE 소비자가 사라진 상태에서 시뮬레이션이 계속 실행되면 큐가 무한 증가 → 메모리 누수. 적어도 `put_nowait` + drop on full 정책이 안전. |
| S2 | L75–L85 `_save_shared_log`/`_save_edges` | wave마다 호출되는데(`_step_agent` L317 turn마다, `run` L448 끝에서 edges), thread pool에서 동시 호출 시 `_file_lock`은 잡히지만 매 턴 전체 json을 통째로 쓰는 비용이 큼 (O(N) per turn). 대안: append-only JSONL. |
| S3 | L410 `ThreadPoolExecutor(max_workers=len(current_wave))` | `current_wave`가 비면 `max_workers=0`이 되어 ValueError. L401 `if not current_wave: break`로 방어되어 있으나 (events_by_wave가 비고 + agent_enter도 없는 경우), agent_enter가 일부 실패하고 next_wave도 비어있어 다시 들어오는 경계에서 확인 필요. |
| S4 | L96–L107 `_resolve_targets` | `t.lower() == "all"`이 한국어/특수 문자에서도 안전(lower는 그대로) — OK. 다만 LLM이 `"All"` 외에도 `"ALL "` 같은 트레일링 공백을 줄 수 있는데 strip 없음. `t.strip().lower()` 권장. |
| S5 | L173–L174 (agent_exit) | exit 후 `self.active_agents.discard(agent_key)`는 OK이나, `_pending_wave`나 `current_wave`에 해당 에이전트가 남아 있어도 정리하지 않음 → 다음 wave에서 비활성 에이전트가 한 번 더 실행될 수 있음. |
| S6 | L290–L296 빈 응답 처리 | `add_to_memory`한 incoming만 pop. 그러나 `add_to_memory`는 L137에서 `self._total_added += 1`도 증가시키므로, 빈 응답 때 _total_added가 과대 집계됨. 카운터의 의미가 "현재 메모리 크기"가 아니라 "총 추가 시도 수"라면 OK이지만, 의도가 코드 주석에 없음. |
| S7 | L210 `key_to_alias=self._key_to_alias` (compress 호출) | `_key_to_alias` 사용은 OK. 그러나 `_compression_agent`가 `wave` 대신 `turn` 값을 넘김(L261). compression_log/episodic_memory의 `wave` 컬럼에 turn 값이 들어가 의미가 어긋남. **데이터 일관성 버그.** |
| S8 | L426–L427 `total_turns += len(current_wave)` | 실패한(`success=False`) 에이전트도 카운트. DB의 `total_turns`는 "완료 턴 수" 의미로 노출되는데, 실패 포함은 오해 소지. |
| S9 | L429–L440 next_wave 구성 | `_resolve_targets`가 active_agents 기준이지만, 같은 wave에서 `agent_exit` 이벤트가 발생해 비활성화된 에이전트는 이미 응답을 한 직후 next_wave에서 자동 제외됨 — OK. 단, agent_enter로 들어온 에이전트는 한 wave 늦게 응답 → 의도된 동작인지 확인 필요. |

---

### 1.3 `ABM/db.py` (551줄)

**A. SRP** — `SimDB` 한 클래스에 다음을 모두 담음:
1. 스키마 정의 + 마이그레이션 (L10–L156)
2. 메시지 보관소 (L175–L197)
3. Episodic memory CRUD (L203–L236)
4. Semantic memory CRUD + 컨피던스 머지 로직 (L242–L305)
5. Relationship memory CRUD (L311–L348)
6. Self-state CRUD (L354–L378)
7. Compression log (L384–L397)
8. 결합 조회 `get_full_memory` (L403–L409)
9. Simulation log per-turn (L415–L452)
10. Simulation run lifecycle + 스냅샷 (L454–L538)
11. Cross-table 삭제 (L540–L551)

**B. 중복** — `conn = self._conn(); now = time.time(); conn.execute(...); conn.commit()` 패턴이 모든 upsert 메서드에 반복. 데코레이터/믹신으로 통일 가능.

**C. 함수 크기** — 개별 메서드는 짧으나 클래스가 551줄. 도메인별 분할 권장.

**D. 결합도**
- `memory_compressor.py`가 `SimDB`를 직접 import (L14)하면서 동시에 read+write 다중 메서드에 의존. Repository 인터페이스(`MemoryRepo`)로 분리하면 압축 로직 단위 테스트가 fixture만으로 가능해짐.

**E. 네이밍** — `sim_id` (메모리 테이블) vs `run_id` (simulation_runs/log/snapshots) 혼용. L548 `for table in tables: ... WHERE sim_id=?` 이지만 `agent_snapshots`는 `run_id`이고 `simulation_log`도 `run_id`인데 L545에 `simulation_log`가 포함되어 있어 **버그**: `simulation_log`는 `run_id` 컬럼이지 `sim_id`가 아님.

**F. 버그 가능성 (실제 발견)**

| # | 위치 | 증상 |
|---|------|------|
| D1 | L540–L551 `delete_run` | tables 목록(L542–L546)에 `simulation_log`가 포함됐는데 `simulation_log` 스키마(L106–L117)는 컬럼명이 **`run_id`**다. `WHERE sim_id=?`로 DELETE가 실행되면 **`no such column: sim_id` 오류**가 발생하거나, 다른 테이블의 인덱스가 무효라 0건 삭제로 조용히 실패. **High 심각도 버그.** |
| D2 | L162–L169 `_conn` | 스레드별 연결을 lazy 생성하지만 종료 훅이 없음 — 백그라운드 스레드가 종료되어도 SQLite 연결은 살아 있다가 GC될 때까지 잠재적 락 유지. |
| D3 | L242–L297 `upsert_facts` | `prev_fact`가 사실 변경을 의미하는데, 이미 `new_fact != prev_fact`인 경우만 변경 처리해야 함. 현 로직은 `new_fact in existing`을 키로 쓰므로 LLM이 `prev_fact`만 채우고 `fact`는 동일하게 보낼 때 변경이 누락된다. 또한 `prev_fact`로 식별하지 않으므로 "이전 사실"이 DB에서 살아남는다. |
| D4 | L478 `run_number = (row["cnt"] or 0) + 1` | 동시 두 `create_run` 호출 시 같은 `run_number`가 발급될 수 있다 (count 기반 race). UNIQUE 제약이 (scenario_id, run_number)에 없어 일관성 깨짐. |
| D5 | L164 `conn = sqlite3.connect(db_path, check_same_thread=False)` | `check_same_thread=False`로 multi-thread 사용 시 명시적 락 책임은 호출자에게 있음. 그러나 `executemany` 도중 다른 스레드가 commit하면 트랜잭션 격리가 흐트러질 수 있다. WAL이라 reader/writer는 OK이지만 writer 동시성은 여전히 직렬화 — 큰 시뮬에서 잠금 대기 가능. |
| D6 | L143 `conn.executescript(_SCHEMA)` | `_SCHEMA` 안의 `PRAGMA journal_mode=WAL` 은 executescript 안에서 트랜잭션 컨텍스트라 적용이 무시될 가능성. 별도 `conn.execute("PRAGMA journal_mode=WAL")`로 들어내야 안전. (실제로 `_conn`에서 다시 적용하므로 큰 문제는 안됨.) |

---

### 1.4 `ABM/agent.py` (183줄)

**A. SRP** — Agent가 (1) 시스템 프롬프트 빌더, (2) 토큰 추정/트림, (3) 파일 로그 작성, (4) 메모리 큐 — 4개 책임. Output Format 생성 로직(L18–L63)은 별도 모듈로 추출 가능.

**B. 중복** — `extra_fields`의 기본값(L12–L16)이 `parser.py` L11–L15와 **동일하게 두 번 정의**. 한 곳이 변경되면 다른 곳을 잊을 위험. → `ABM/constants.py`로 단일화 필요.

**C. 함수 크기** — 짧고 깔끔, 함수당 30줄 미만.

**D. 결합도** — 낮음. 다만 `_init_log_file`이 생성자에서 파일을 강제로 비우므로 (L99–L102), Agent를 임의로 두 번 생성하면 이전 로그가 날아감. 테스트 시 주의.

**E. 네이밍** — `_total_added`, `_trimmed_count`, `_last_prompt_tokens` private인데 `_step_agent`에서 직접 set/read. `Agent`가 명시적 API를 노출하지 않아 캡슐화가 새는 형태.

**F. 버그 가능성**

| # | 위치 | 증상 |
|---|------|------|
| A1 | L66–L68 `_estimate_tokens` | `len(text.encode("utf-8")) // 4` — 한국어 UTF-8은 글자당 3바이트라 토큰을 과대 추정. tokenizer 호출이 없으면 보수적이라 안전한 쪽이긴 함. 다만 vLLM `prompt_tokens` 응답으로 보정(L300) 시 큰 격차가 생길 수 있다 — 압축 트리거(L260)는 estimate 기반이므로 실제 토큰의 1.5~2배에서 발동. |
| A2 | L156–L160 `trim_to_token_limit` | while 루프 안에서 매번 `estimate_context_tokens`를 재계산 (build_messages 포함) → 메모리가 큰 경우 O(N²). |
| A3 | L100 `os.makedirs(os.path.dirname(self.log_file), exist_ok=True)` | 동일한 log_file 경로가 두 Agent에 들어가면 마지막 init이 이긴다 — 이름 중복 검증 없음. |
| A4 | L103–L120 `add_to_log` | 매 호출마다 파일을 통째로 다시 씀. 턴이 많아질수록 비용 증가. |
| A5 | L160 `self.memory.pop(0)` | 리스트 head pop은 O(N). deque 사용 권장. |

---

### 1.5 `ABM/llm.py` (47줄)

- 짧고 깔끔. 다만:
  - L28 `max_tokens=16384` 하드코딩 — 시나리오마다 다르게 필요할 수 있음. 인자로 받아야 함.
  - L29 `temperature=0.7` 하드코딩 — 동상.
  - L10–L14 retry: `requests.exceptions.RequestException`만 retry되며, HTTP 5xx는 `raise_for_status`가 `HTTPError`(`RequestException` 하위)라 OK. 단, vLLM이 빈 choices를 반환하면 `ValueError` raise되고 retry되지만 같은 결과가 반복될 가능성.

---

### 1.6 `ABM/memory_compressor.py` (219줄)

- 응집도 양호. 단일 책임(압축).
- L60–L82 `_format_existing`, L106–L147 `build_memory_block` — 출력 포맷이 거의 동일(서식만 조금 다름). 두 함수를 한 함수로 합치고 옵션 인자로 분기 가능.
- L96–L99 `_parse_compression_result` — 마크다운 펜스만 제거. `parser.py`의 `_CODE_FENCE_RE`와 동일한 로직 — 공통 유틸로 추출 가능.
- L185–L196 try/except: 실패 시 None 반환은 OK이지만 LLM 응답이 부분적으로 유효(예: episodes만 있고 self_state 누락)일 때도 통째로 None 반환 → 데이터 회수율 손실.

---

### 1.7 `ABM/parser.py` (56줄)

- 짧고 명확.
- L11–L15 `_DEFAULT_EXTRA_FIELDS`가 `agent.py` L12–L16과 중복 (재언급).
- L41–L50 targets 분기: `"system"`/`"all"`/콤마 분리/빈 값 → `["system"]`. 안정적이지만 단일 토큰 `"boss"`도 콤마 분리 통과해서 `["boss"]`가 되니 의도대로 동작 OK.

---

### 1.8 `frontend/js/simulation.js` (1,484줄) — **가장 시급한 분할 대상**

**A. SRP — 심각하게 위반**
- 한 파일이 다음 13개 책임을 가짐:
  1. 모듈 상태 `sim` + 액센트 helper (L4–L36)
  2. View 전환 (L39–L72)
  3. Settings 페이지 렌더 (L74–L87)
  4. Output Fields editor (L90–L123)
  5. Agent accordion editor (L126–L276)
  6. Agent Cards (런 뷰) 렌더링 (L288–L354)
  7. Feed/Typing indicator (L357–L420)
  8. SSE 핸들러 (L430–L499)
  9. Start/Stop/Continue control (L502–L589)
 10. Scenarios CRUD UI (L592–L671)
 11. Run history modals & replay (L674–L946)
 12. D3 Force Graph (L1003–L1106)
 13. Scenario events editor + 컨텍스트 윈도우 + resize + init (L1118–L1485)

**B. 중복**
- `fmtTime` 헬퍼가 L696–L701, L755–L760, L835–L840 **세 곳에** 동일 정의 (약간의 포맷만 다름).
- `statusIcon` 객체가 L695, L754, L834 **세 곳에** 동일 정의.
- Replay 모달 메타 배지 렌더링(L884–L890)이 `addFeedMessage`(L401–L403)와 유사한 패턴.
- "JSON parse with code fence 제거" 로직이 L1339–L1342에 인라인 — `ABM/parser.py`의 백엔드 로직과 거울 (프론트–백엔드 양쪽에 같은 파싱을 유지).
- D3 노드/링크/마커 추가 코드(L1060–L1106)와 `initD3Graph` (L1007–L1051)에 마커 정의가 분산.

**C. 파일 크기** — 1,484줄. ESM이지만 모듈 단일 파일로는 너무 큼.

**D. 결합도**
- `sim` (모듈 전역)을 전역 가변 상태로 사용. 어떤 함수가 상태를 변형하는지 추적이 어렵다.
- `_expandedAgents` set과 `_d3Sim`, `_d3Data` 등 별도의 전역도 산재.

**E. 네이밍**
- camelCase + 한국어 코멘트 혼용. UI 라벨 한국어는 OK.
- `simc-*` (sim card prefix), `sim-acrd-*` (accordion prefix), `sim-feed-*` (피드 메시지) 접두어가 일관됨 → 좋음.
- `connectSSE`, `addD3Edge`, `openRunReplay` 등 동사–명사 명확.

**F. 버그 가능성 (실제 발견)**

| # | 위치 | 증상 |
|---|------|------|
| F1 | L295 `card.id = \`simc-${agent.name}\`` | agent.name이 영문 ID라 가정. 사용자가 한국어/특수문자 입력 시 DOM ID로 부적합 → `getElementById` 실패. 백엔드는 한국어 ID도 허용(L46 AgentConfig.name: str)이라 갭. |
| F2 | L431, L549 `sim.eventSource = null` | `setStatus('stopped')`를 호출하기 전에 SSE close → `simulation_end` 이벤트가 도착하기 전에 다른 코드에서 `sim.eventSource`를 참조하면 null. |
| F3 | L457–L459 | `document.getElementById('sim-tab-context').classList.contains('sim-hidden')` — 만약 'sim-tab-context' 요소가 아직 렌더되지 않은 시점(view 미진입)에서 turn_complete가 오면 TypeError. |
| F4 | L391–L420 `addFeedMessage` | `esc(data.content)`로 XSS 방어 OK. 그러나 L412 `actionNote` 도 `esc` 적용됨. ✓. 하지만 L416 `data.reasoning_preview`는 모델이 임의 텍스트를 줄 수 있는데 `esc` 적용됨 ✓. 종합적으로 innerHTML 패턴은 모두 `esc` 처리됨. |
| F5 | L1170–L1172 | `value="${esc(ev.message || '')}"` — innerHTML로 input value를 세팅. esc 잘 됨. |
| F6 | L985 `sim.token_limit = cfg.token_limit ?? (cfg.memory_limit ? cfg.memory_limit * 400 : 8192)` | 옛 시나리오 호환은 OK. 단 `??` 사용으로 cfg.token_limit이 0일 경우 fallback이 안 됨 — 의도일 수도 있지만 명시 주석 필요. |
| F7 | L741, L814, L919 `await fetch(...)` 후 `.json()` 미체크 | 응답이 204(No Content)인데 `.json()`을 시도하면 throw — DELETE는 204 (백엔드 L484, L562)라 응답 본문 없음. 다만 코드에서 `.json()`을 호출하지 않으므로 OK. resume(L919)은 200으로 가정 — 백엔드는 `{"status":"resuming"}` 반환 ✓. |
| F8 | L1339–L1342 | `if (raw.includes('```json'))`으로 분기 — assistant 메시지의 `content`가 `\`\`\`json`이 포함된 평문이면 오인 파싱. `parse_json_response`처럼 시작/끝 앵커 정규식 사용이 안전. |
| F9 | L1085 `EMOTION_COLORS[d.emotion] ? d.emotion : 'default'` | meta.emotion이 임의 문자열이면 default 마커로 떨어짐 ✓. |
| F10 | L455 `edge.emotion || (edge.meta || {}).emotion || 'neutral'` | 백엔드 `new_edges`는 항상 emotion 키를 채워 보냄(simulation.py L327) → 폴백 코드는 dead. |
| F11 | L928, L940 `applyScenario({ id: run.scenario_id, ... })` | resume/restart 시 시나리오 객체를 즉석 합성. `run.scenario_id`가 null이면 sim.currentScenarioId가 null로 세팅돼 이력 버튼 disable. 의도 확인 필요. |

---

### 1.9 `backend/main.py` (40줄)

- 짧고 깔끔. 모든 라우터가 등록됨 (L33–L38). ✓ `simulation.router` 포함.
- 단 한 가지: `app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")` — 프로세스 cwd가 프로젝트 루트가 아니면 404. README/실행 가이드에 cwd 의존성 명시 필요.

---

### 1.10 `backend/state.py` (1줄)

- `max_model_len: int = 0` 한 줄. 글로벌 변수만 노출 — chat 라우터들이 이걸 import해서 쓰는 패턴. 시뮬레이션은 사용하지 않음. 향후 simulation token_limit과 통합 시 참고.

---

## 2. 모듈화 계획

각 항목은 **현재 위치 → 추출 위치 → 이유 → 우선순위 → 위험도** 형식.

### [High] H1. `frontend/js/simulation.js` 분할

- **현재**: `frontend/js/simulation.js` (L1–L1484)
- **추출 위치**:
  - `frontend/js/sim/state.js` — `sim`, `_expandedAgents`, `EMOTION_COLORS`, helpers (L1–L36)
  - `frontend/js/sim/views.js` — show/hide view 함수 (L39–L72)
  - `frontend/js/sim/settings/output-fields.js` — Output Fields editor (L90–L123)
  - `frontend/js/sim/settings/agents.js` — Agent accordion (L126–L286)
  - `frontend/js/sim/settings/events.js` — Scenario events editor (L1118–L1206)
  - `frontend/js/sim/run/cards.js` — Agent Cards (L288–L354)
  - `frontend/js/sim/run/feed.js` — Feed/Typing/SceneEvent (L357–L420, L1207–L1225)
  - `frontend/js/sim/run/sse.js` — SSE 연결 (L430–L499)
  - `frontend/js/sim/run/control.js` — start/stop/continue (L502–L589)
  - `frontend/js/sim/scenarios.js` — Scenarios CRUD (L592–L671)
  - `frontend/js/sim/runs/history.js` — Run history panel & all-runs modal (L674–L819)
  - `frontend/js/sim/runs/replay.js` — Replay modal (L821–L946)
  - `frontend/js/sim/graph/d3.js` — D3 force graph (L1003–L1116)
  - `frontend/js/sim/context.js` — Context window (L1241–L1373)
  - `frontend/js/sim/utils/time.js` — `fmtTime`, `statusIcon` (중복 제거)
  - `frontend/js/sim/index.js` — 기존 `initSimulationEvents` 만 남김
- **이유**: SRP, 파일 크기(1,484줄), 중복(`fmtTime` 3회), 결합도(전역 `sim`), 유지보수성.
- **우선순위**: High — 가장 빠르게 가치 회수 가능.
- **위험**:
  - import 경로 다수 변경 → index.html의 `<script type="module" src="js/main.js">`만 진입점이라 그대로 OK이지만 main.js가 simulation을 import한다면 그 한 경로 유지.
  - 전역 `sim` 객체를 모듈 간 공유하려면 `state.js`에서 export하고 다른 모듈에서 named import. 순환 의존성 주의.
  - D3 노드 좌표 등 런타임 상태가 모듈 간 분리되면서 race 발생 가능 — 모든 모듈이 같은 `_d3Sim` 인스턴스를 보도록 single export 보장.

### [High] H2. `backend/api/simulation.py` 분할 + 공통 추출

- **현재**: `backend/api/simulation.py` (L1–L567)
- **추출 위치**:
  - `backend/api/simulation/__init__.py` — APIRouter 통합
  - `backend/api/simulation/schemas.py` — Pydantic 모델 (L45–L92)
  - `backend/api/simulation/state.py` — `_sim` dict + `_sim_lock`(신규) + helpers
  - `backend/api/simulation/runtime.py` — `start`/`continue`/`resume` 컨트롤러
  - `backend/api/simulation/runner.py` — 시뮬레이션 실행 스레드 부트스트랩 (3개 `_run` 통합 후 분기 인자로)
  - `backend/api/simulation/sse.py` — `_blocking_get`, `stream_events`
  - `backend/api/simulation/runs.py` — Run CRUD
  - `backend/api/simulation/scenarios.py` — Scenario CRUD
- **이유**: SRP, 함수 크기, 3개 `_run` 함수 중복, 글로벌 상태 격리, 테스트 용이성.
- **우선순위**: High — 동시성 버그(B1, B2)의 수정과 함께 묶어 진행하면 효율적.
- **위험**:
  - 글로벌 `_sim` dict가 여러 모듈에서 참조되어야 함 → `state.py`로 옮기고 import. SSE generator가 사용하는 큐 객체의 lifetime 추적 필요.
  - 라우터 prefix와 path가 동일하게 유지되어야 클라이언트 호환. 분할 시 `APIRouter(prefix="/api/simulation")` 한 곳에서 sub-router include 패턴 권장.

### [High] H3. `ABM/db.py` 분할 + bug D1 수정

- **현재**: `ABM/db.py` (L1–L551)
- **추출 위치**:
  - `ABM/db/__init__.py` — `SimDB` 파사드 (현 API 호환 유지)
  - `ABM/db/schema.py` — `_SCHEMA`, `_migrate`
  - `ABM/db/conn.py` — `_local`, `_conn()`
  - `ABM/db/messages.py` — `save_messages`
  - `ABM/db/episodic.py` — `upsert_episodes`, `get_episodes`
  - `ABM/db/semantic.py` — `upsert_facts`, `get_facts`
  - `ABM/db/relationship.py` — relationship + history
  - `ABM/db/self_state.py` — self_state
  - `ABM/db/runs.py` — simulation_runs/log/snapshots
- **이유**: 클래스 551줄, 도메인 분리, D1 같은 컬럼명 불일치 버그가 한 곳에 모여 있어 fan-out 위험.
- **우선순위**: High — D1 버그 수정과 함께.
- **위험**:
  - `SimDB` 단일 임포트가 코드베이스 다수 위치에서 사용 → 파사드 패턴으로 외부 API 유지 필요(`from ABM.db import SimDB`).
  - 마이그레이션 로직(L148–L156)은 첫 connection 시 한 번만 실행되므로 분할 후에도 순서 보장.

### [High] H4. `ABM/simulation.py` 의 `_step_agent` 분해

- **현재**: `ABM/simulation.py` L223–L365 (143줄)
- **추출 위치** (동일 파일 내 또는 `ABM/turn.py`로):
  - `_inject_incoming(agent, incoming)` — incoming → memory
  - `_maybe_compress(agent, key, wave, other_agents)` — 압축 트리거
  - `_call_llm(agent, messages)` — LLM 호출 + 에러 매핑
  - `_apply_turn_result(agent, key, raw, reasoning, usage, wave, turn)` — 파싱 + 메모리/엣지/로그/emit
- **이유**: SRP, 함수 크기, 테스트 가능성. 압축 트리거의 `wave`/`turn` 인자 혼동(S7) 같은 버그가 함수 분리 시 자연스럽게 드러남.
- **우선순위**: High — 컴포넌트 단위 테스트가 가능해짐.
- **위험**:
  - 부분 실패(incoming pop) 복원 로직이 한 함수에서 다른 함수로 이동하면 traceability 손실 → docstring으로 라이프사이클 명시.

### [Medium] M1. ABM 공유 상수 통합

- **현재**:
  - `ABM/agent.py` L12–L16 `_DEFAULT_EXTRA_FIELDS`
  - `ABM/parser.py` L11–L15 `_DEFAULT_EXTRA_FIELDS`
  - `ABM/agent.py` L18–L32 `DEFAULT_OUTPUT_FORMAT_TEMPLATE`
- **추출 위치**: `ABM/constants.py`
- **이유**: 중복 정의, 동기화 누락 위험.
- **우선순위**: Medium — 변경 빈도가 낮지만 한 번 변경되면 두 곳을 수정해야 해 위험.
- **위험**: 백엔드/프론트가 `default-output-format` 엔드포인트(simulation.py L510)로 템플릿을 가져오는데, 이 import 경로가 바뀌어도 라우터에서 정상 import 되도록 확인.

### [Medium] M2. SimDB → Repository 인터페이스 분리

- **현재**: `ABM/memory_compressor.py`가 `from .db import SimDB`로 직접 결합 (L14).
- **추출 위치**: `ABM/memory_repo.py` — `MemoryRepo` 추상 인터페이스 (get/upsert episodes/facts/relationships/self_state) + `SimDBMemoryRepo` 구현.
- **이유**: 압축 단위 테스트가 in-memory fixture로 가능해짐.
- **우선순위**: Medium — 테스트 인프라가 갖춰진 뒤가 효율적.
- **위험**: 메서드 시그니처 변경 시 호출부(`Simulation._compress_agent` 등) 동기화.

### [Medium] M3. SSE 이벤트 페이로드 빌더

- **현재**: `ABM/simulation.py`가 `_emit("turn_start", {...})`, `_emit("turn_complete", {...})` 등을 메서드 내부에서 dict 리터럴로 구성.
- **추출 위치**: `ABM/events.py` — `make_turn_start_event(...)`, `make_turn_complete_event(...)` 등 dataclass+to_dict.
- **이유**: 프론트–백엔드 페이로드 shape 변경 시 한 곳만 수정. 타입 안정성.
- **우선순위**: Medium — 인터페이스 안정성이 중요해질 때.
- **위험**: 프론트 `simulation.js` SSE 핸들러(L436–L499)에서 사용하는 필드명과 정확히 매핑 유지.

### [Medium] M4. 백엔드–프론트 시각 표시 헬퍼 통합

- **현재**: `fmtTime`, `statusIcon`이 `simulation.js`에 3중 정의 (L695–L701, L754–L760, L834–L840).
- **추출 위치**: `frontend/js/sim/utils/time.js`
- **이유**: 중복 제거.
- **우선순위**: Medium — H1과 함께 수행.
- **위험**: 출력 포맷이 미세하게 달랐던 곳(`refreshRunHistory`는 MM-DD HH:MM, `openAllRunsModal`/`openRunReplay`는 YYYY-MM-DD HH:MM) → `fmtTime(ts, {includeYear: true})` 같은 옵션으로 통합 필요.

### [Medium] M5. Race condition / cross-run leak 수정

- **현재**: `backend/api/simulation.py` L107–L122 (B1), L292–L315 (B2)
- **추출 위치**: `backend/api/simulation/state.py`에서 `_sim_lock` 도입, `event_queue` 소유권을 (sim_run_id → Queue) 매핑으로 변경.
- **이유**: 동시 요청에서 두 시뮬레이션이 동시에 실행되거나, 이전 큐를 새 SSE 소비자가 잡는 버그.
- **우선순위**: Medium — 실제 운영에서 동시 사용자가 적다면 우선순위 낮으나 데모/E2E 자동화 시 즉시 노출됨.
- **위험**: 큐 분리 시 stream_events가 어떤 큐를 잡을지 결정 — run_id 쿼리 파라미터 또는 최신 active run 가져오는 정책 필요.

### [Low] L1. `Agent` private 속성 → property/메서드 노출

- **현재**: `_total_added`, `_trimmed_count`, `_last_prompt_tokens`, `_token_limit`, `_memory_block`이 `Simulation`에서 외부 접근.
- **추출 위치**: `ABM/agent.py` — `@property`로 read-only 노출.
- **이유**: 캡슐화.
- **우선순위**: Low — 동작에 영향 없으나 리팩토링 시 검색 용이.
- **위험**: 거의 없음.

### [Low] L2. ABM JSON 파일 I/O를 비동기/배치로

- **현재**: `ABM/simulation.py` L317 `_save_shared_log` 매 turn 호출, `ABM/agent.py` L119–L120 `add_to_log` 매 turn 파일 전체 재쓰기.
- **추출 위치**: `ABM/persist.py` — debounced writer / append-only JSONL.
- **이유**: 성능 (S2, A4).
- **우선순위**: Low — 작은 시뮬에서는 무시 가능.
- **위험**: 갑작스러운 종료 시 마지막 버퍼 분실 가능 — flush hook 필요.

### [Low] L3. Markdown 코드펜스 제거 유틸 통합

- **현재**:
  - `ABM/parser.py` L9 `_CODE_FENCE_RE`
  - `ABM/memory_compressor.py` L94–L99 `_parse_compression_result`
  - `frontend/js/simulation.js` L1339–L1342 (assistant content 파싱)
- **추출 위치**: `ABM/json_util.py` (백엔드) + `frontend/js/sim/utils/json.js` (프론트)
- **이유**: 동일 정규식 로직 분산.
- **우선순위**: Low.
- **위험**: 거의 없음.

### [Low] L4. ABM/config.py 하드코딩 모델명 정정

- **현재**: `ABM/config.py` L4 `MODEL = "Qwen/Qwen3.6-35B-A3B"` — 오타로 의심되는 모델명(`Qwen3.6`은 실제 존재하지 않으며 `Qwen3-30B-A3B`나 유사 모델을 의도한 듯).
- **추출 위치**: 코드 그대로 두되 검증.
- **이유**: 환경 변수가 없을 때 잘못된 디폴트로 LLM 호출 실패.
- **우선순위**: Low — 환경에서 `VLLM_MODEL` 설정 시 무관.
- **위험**: 없음.

---

## 3. 실제 발견된 즉시 수정 권장 버그 (우선순위 순)

| 우선 | ID | 위치 | 증상 | 수정 스니펫 |
|------|----|------|------|--------------|
| P0 | D1 | `ABM/db.py` L540–L551 | `simulation_log`는 `run_id` 컬럼이지만 `WHERE sim_id=?`로 삭제 시도 → 에러 또는 조용한 실패 | `tables` 목록에서 `simulation_log` 제외하고 별도 `DELETE FROM simulation_log WHERE run_id=?` 추가 |
| P0 | B1 | `backend/api/simulation.py` L107–L122 | 동시 `/start` 호출 시 두 워커 스레드가 동시 시작 가능 | 모듈 전역 `_sim_lock = threading.Lock()` + `with _sim_lock:` 안에서 체크–세팅 |
| P0 | B4 | `backend/api/simulation.py` L194–L199 | 예외 경로에서 `db` 또는 `run_sim_id`가 미정의이면 `NameError` | `_run` 첫 줄에 `db = None; run_sim_id = None`로 선언 |
| P1 | S7 | `ABM/simulation.py` L261 | `_compress_agent(active_agent, agent_key, turn)` — `turn` 자리에 `wave` 의도였을 가능성. 압축 로그 wave 컬럼에 turn 값 저장됨 | `self._compress_agent(active_agent, agent_key, wave)` |
| P1 | B2 | `backend/api/simulation.py` L292–L315 | 이전 sim의 큐를 다음 sim 시작 후 SSE 소비자가 잡거나, 중복 SSE 소비자가 sentinel 미수신 | `event_queue`를 sim 인스턴스별 dict (key=run_id)에 저장 + `/stream`에서 run_id로 조회 |
| P1 | S5 | `ABM/simulation.py` L173–L188 | `agent_exit` 시 `_pending_wave`/`current_wave`에서 해당 키 제거 안 됨 → 비활성 에이전트가 한 번 더 실행 가능 | exit 분기에서 `self._pending_wave.pop(agent_key, None)` 추가, run 루프에서 다음 wave 진입 시 `active_agents` 필터링 |
| P1 | D4 | `ABM/db.py` L478 | 동시 create_run이 같은 run_number 발급 | (scenario_id, run_number)에 UNIQUE 인덱스 + INSERT 재시도, 또는 `INSERT ... RETURNING` 사용 |
| P2 | F1 | `frontend/js/simulation.js` L295 | agent.name이 한국어/특수문자면 DOM ID 사용 불가 | 백엔드 schema에 영문 ID 검증 추가, 또는 프론트에서 `CSS.escape` 사용 |
| P2 | F8 | `frontend/js/simulation.js` L1339–L1342 | 평문에 `\`\`\`json`이 포함되면 오인 파싱 | `^\\s*\`\`\`(?:json)?` 앵커 정규식 사용 |
| P2 | B9 | `backend/api/simulation.py` L455 | resume 시 wave 0부터 이벤트 재실행 | resume의 events는 `saved_pending.wave` 이후로 필터링, 또는 cfg.events 무시 |
| P3 | D3 | `ABM/db.py` L242–L297 | prev_fact 기반 사실 변경이 누락될 수 있음 | upsert_facts 로직을 prev_fact가 있으면 prev_fact를 기존 fact 키로 조회하도록 변경 |

---

## 4. 레이어별 담당 에이전트

| 레이어 | 담당 | 작업 항목 |
|--------|------|----------|
| ABM 모듈화 (H3, H4, M1, M2, M3, L1, L2, L3) + 버그 D1/D3/D4/S5/S7 | **abm-engineer** (또는 `abm-skill` 기반 backend-dev) | `ABM/db.py` 분할 + 컬럼명 통일, `_step_agent` 분해, 공유 상수 통합 |
| 백엔드 모듈화 (H2, M5) + 버그 B1/B2/B4/B9 | **backend-dev** | `backend/api/simulation.py` 패키지화, race condition fix, SSE 큐 격리 |
| 프론트엔드 모듈화 (H1, M4, L3) + 버그 F1/F8 | **frontend-dev** | `frontend/js/simulation.js` 13개 모듈 분할, 시간/상태 헬퍼 통합, 코드펜스 정규식 강화 |
| QA & 통합 검증 | **qa-reviewer** | 분할 후 SSE 페이로드 shape 교차 검증, regressions 테스트 추가 |

### 권장 진행 순서

1. **Sprint 1 (Bug fix only)** — P0/P1 버그를 모듈화 없이 핀포인트 수정. 회귀 테스트 추가. 작업량 1–2일.
2. **Sprint 2 (Backend split)** — H2 + H3 (백엔드 + ABM/db 분할). M5 race condition 함께. 작업량 2–3일.
3. **Sprint 3 (ABM core split)** — H4 + M1 + M2 + M3 (ABM 시뮬레이션 코어 분할 + Repository 인터페이스). 작업량 2–3일.
4. **Sprint 4 (Frontend split)** — H1 + M4 + L3. 작업량 3–4일 (1,484줄 가장 큼).
5. **Sprint 5 (성능/캡슐화 cleanup)** — L1, L2, L4. 작업량 1일.

각 Sprint 후 `qa-reviewer`가 SSE 페이로드 / DB 컬럼 / Pydantic schema의 경계면을 교차 검증.
