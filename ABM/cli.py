"""GUI 없이 시나리오를 돌리고 스크린플레이 마크다운을 얻는 CLI.

같은 시나리오를 N회 반복하거나, 파라미터를 바꿔가며 스윕하거나, 회귀 평가를
자동화할 때 쓴다. 브라우저에서 하던 "시작 → 지켜보기 → 마크다운 내보내기"를
명령 한 줄로 접은 것이며, 실행 경로(``ABM/simulation/headless.run_config``)와
문서 포맷(``ABM/export/markdown.render_markdown``)을 GUI 와 **공유**한다.

    # 리포지토리 루트에서 실행할 것 (memory.db · servers.json · logs_graph 경로가 상대경로)
    cd /path/to/vllm

    # 설정만 검증하고 엔진이 주입할 프롬프트 계약을 확인
    python -m ABM.cli run scenario.json --dry-run

    # 한 번 실행 → 파일로 (기본 파일명: {시나리오명}_{YYYY-MM-DD_HHMM}.md)
    python -m ABM.cli run scenario.json

    # stdout 으로
    python -m ABM.cli run scenario.json -o -

    # 같은 시나리오 20회, 4개씩 병렬 → results/ 에 20개 마크다운
    python -m ABM.cli run scenario.json -n 20 --outdir results/ --concurrency 4

    # DB에 저장된 시나리오를 id 로 실행 + 설정 오버라이드
    python -m ABM.cli run --scenario-id <uuid> --max-waves 200 --server-id <server>

    # 이미 끝난 실행(GUI 실행 포함)을 마크다운으로 다시 뽑기
    python -m ABM.cli export --run-id <uuid> -o out.md
    python -m ABM.cli export --scenario "4인가족" --latest

시나리오 파일(``scenario.json``)은 세 가지 모양을 모두 받는다.
  1. ``SimStartConfig`` 그 자체 (``{"agents": [...], "background": ..., ...}``)
  2. 시나리오 저장 형식 (``{"name": ..., "config": {...}}``) — ``name`` 이 문서 제목이 된다
  3. ``/api/simulation/scenarios`` 응답 항목 (``{"name": ..., "config_json": "..."}``)

**토글** (``--include`` / ``--exclude``, 쉼표 구분):
``time`` ``action`` ``move`` ``appearance`` ``world`` ``intervention``
``infection`` ``meeting`` ``summary``. 기본값은 GUI 체크박스와 같이 ``summary`` 만 꺼짐.

**종료 코드**
  0  정상 (``end_reason`` 이 무엇이든)
  1  ``--strict`` 이고 ``end_reason`` 이 ``silence`` / ``no_agents``
  2  설정·입출력 오류 (파일 없음, JSON 파싱 실패, 스키마 위반, 알 수 없는 토글)
  3  실행 중 예외 (LLM 연결 실패 등)

**주의**
  - LLM 서버는 채팅 DB(``memory.db``)의 ``servers`` 테이블에서 읽는다. 비어 있으면
    리포지토리 루트의 ``servers.json`` 으로 시드한다 — 백엔드 기동과 같은 규칙이라
    GUI 에서 쓰던 서버 설정을 그대로 쓴다.
  - run 마다 에이전트 로그(json)는 임시 디렉토리에 격리된다. ``ABM_LOG_DIR``
    (기본 ``logs_graph``)은 ``simulation.db`` 경로로만 쓰인다.
  - ``--no-db`` 가 아니면 실행이 ``simulation.db`` 에 기록돼 GUI 의 실행 이력·
    불러오기·인터뷰에서 그대로 보인다.
  - ``--seed`` 는 엔진의 ``random`` 사용처(가변 시간 모드의 구간 샘플링, 감염 판정)
    만 고정한다. LLM 응답 자체는 여전히 변동하므로 완전 재현은 되지 않는다.
    전역 ``random`` 을 건드리므로 ``--concurrency`` > 1 과 함께 쓰면 무의미하다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import signal
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


# ── 종료 코드 ─────────────────────────────────────────────────────────────────

EXIT_OK      = 0
EXIT_STRICT  = 1
EXIT_CONFIG  = 2
EXIT_RUNTIME = 3

_STRICT_END_REASONS = {"silence", "no_agents"}


class ConfigError(Exception):
    """설정·입출력 오류 → 종료 코드 2."""


# ── 파일명 (frontend/js/sim/utils/download.js 와 같은 규칙) ────────────────────

_UNSAFE_FILENAME = re.compile(r'[/\\:*?"<>|]')


def safe_filename(s: str) -> str:
    return _UNSAFE_FILENAME.sub("_", s)[:80]


def now_tag() -> str:
    """``2026-09-03_0550`` — JS ``nowTag()`` 와 같이 **UTC** 기준이다."""
    return datetime.now(timezone.utc).isoformat()[:16].replace("T", "_").replace(":", "")


# ── 설정 로딩 ─────────────────────────────────────────────────────────────────

def _unwrap_scenario(raw: dict) -> tuple[dict, str]:
    """시나리오 파일의 세 가지 모양을 (config dict, name) 으로 정규화."""
    if not isinstance(raw, dict):
        raise ConfigError("시나리오 JSON 의 최상위는 객체여야 합니다.")
    name = str(raw.get("name") or "")
    if isinstance(raw.get("config"), dict):
        return raw["config"], name
    if isinstance(raw.get("config_json"), str):
        try:
            return json.loads(raw["config_json"]), name
        except json.JSONDecodeError as e:
            raise ConfigError(f"config_json 을 파싱할 수 없습니다: {e}") from e
    if "agents" not in raw:
        raise ConfigError(
            "시나리오 JSON 에서 설정을 찾지 못했습니다 — "
            "'agents' 를 가진 SimStartConfig 이거나 'config'/'config_json' 을 담은 "
            "시나리오 저장 형식이어야 합니다."
        )
    return raw, name


def _load_scenario(path: str | None, scenario_id: str | None) -> tuple[dict, str]:
    if scenario_id:
        from backend.db.database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT name, config_json FROM simulation_scenarios WHERE id=?",
                (scenario_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ConfigError(f"시나리오를 찾을 수 없습니다: {scenario_id}")
        try:
            data = json.loads(row["config_json"])
        except json.JSONDecodeError as e:
            raise ConfigError(f"저장된 config_json 을 파싱할 수 없습니다: {e}") from e
        data.setdefault("scenario_id", scenario_id)
        return data, row["name"]

    if not path:
        raise ConfigError("scenario.json 경로 또는 --scenario-id 가 필요합니다.")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise ConfigError(f"시나리오 파일을 읽을 수 없습니다: {e}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"시나리오 JSON 파싱 실패: {e}") from e
    data, name = _unwrap_scenario(raw)
    if not name:
        name = os.path.splitext(os.path.basename(path))[0]
    return data, name


def _build_config(args):
    """시나리오 dict + CLI 오버라이드 → 검증된 ``SimStartConfig``."""
    from pydantic import ValidationError
    from backend.api.simulation.schemas import SimStartConfig

    data, name = _load_scenario(getattr(args, "scenario", None), args.scenario_id)
    data = dict(data)
    # 구 시나리오 스냅샷에만 있는 폐기 필드 — 런타임이 무시하지만 남겨둬도 무해하다.
    if args.max_waves is not None:
        data["max_waves"] = args.max_waves
    if args.target_minutes is not None:
        data["target_duration_minutes"] = args.target_minutes or None
    if args.server_id is not None:
        data["server_id"] = args.server_id or None
    if args.temperature is not None:
        data["temperature"] = args.temperature
    if args.step_delay is not None:
        data["step_delay"] = args.step_delay

    try:
        cfg = SimStartConfig(**data)
    except ValidationError as e:
        raise ConfigError(f"시나리오 설정이 스키마를 만족하지 않습니다:\n{e}") from e
    return cfg, (name or "시나리오")


def _parse_include(args) -> frozenset:
    from ABM.export.markdown import DEFAULT_INCLUDE, INCLUDE_KEYS

    def _split(v):
        return [t.strip() for t in (v or "").split(",") if t.strip()]

    if args.include:
        keys = set(_split(args.include))
    else:
        keys = set(DEFAULT_INCLUDE)
    keys -= set(_split(args.exclude))
    unknown = keys - set(INCLUDE_KEYS)
    if unknown:
        raise ConfigError(
            f"알 수 없는 토글: {', '.join(sorted(unknown))} "
            f"(가능: {', '.join(INCLUDE_KEYS)})"
        )
    return frozenset(keys)


# ── LLM 런타임 부트스트랩 ─────────────────────────────────────────────────────

class LLMRuntime:
    """백엔드 lifespan 없이 provider 계층을 쓰기 위한 최소 부트스트랩.

    ``backend/llm/bridge.py`` 의 동기 어댑터는 provider 코루틴을 **메인 이벤트
    루프**(``backend.state.event_loop``)로 위탁 실행한다. FastAPI 밖에서는 그 루프가
    없으므로 여기서 전용 루프를 백그라운드 스레드에 띄우고 참조를 심어준다.
    서버 목록은 백엔드 기동과 같은 순서로 채팅 DB → ``servers.json`` 시드에서 읽는다.
    """

    def __init__(self):
        self._loop = None
        self._thread = None

    def __enter__(self):
        from backend import state as backend_state
        from backend.db.database import (
            get_db, init_tables, migrate_db, seed_default_servers,
        )
        from backend.llm import client as llm_client
        from backend.llm.registry import get_registry

        # sqlite3 커넥션은 만든 스레드에서만 쓸 수 있으므로 DB 준비 전체를 루프
        # 스레드 안에서 돌린다 — 백엔드 lifespan 이 하는 순서 그대로다.
        async def _bootstrap():
            conn = get_db()
            try:
                init_tables(conn)
                migrate_db(conn)
                seed_default_servers(conn)
                await llm_client.setup(conn)
            finally:
                conn.close()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        backend_state.event_loop = self._loop

        try:
            asyncio.run_coroutine_threadsafe(_bootstrap(), self._loop).result()
            if not get_registry().list_providers():
                raise ConfigError(
                    "사용 가능한 LLM 서버가 없습니다 — memory.db 의 servers 테이블이 비어 있고 "
                    "servers.json 도 없습니다. 리포지토리 루트에서 실행했는지 확인하세요."
                )
        except BaseException:
            self.__exit__(None, None, None)   # 루프 스레드를 남기지 않는다
            raise
        return self

    def __exit__(self, *exc):
        from backend import state as backend_state
        from backend.llm import client as llm_client
        try:
            asyncio.run_coroutine_threadsafe(
                llm_client.teardown(), self._loop).result(timeout=30)
        except Exception:
            pass
        backend_state.event_loop = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:
            pass
        return False


# ── 진행률 ────────────────────────────────────────────────────────────────────

class Progress:
    """stderr 진행률. 여러 run 이 동시에 써도 줄이 섞이지 않도록 락을 건다."""

    def __init__(self, quiet: bool, total_runs: int, max_waves: int):
        self.quiet = quiet
        self.total_runs = total_runs
        self.max_waves = max_waves
        self._lock = threading.Lock()

    def say(self, msg: str) -> None:
        if self.quiet:
            return
        with self._lock:
            print(msg, file=sys.stderr, flush=True)

    def callback(self, idx: int):
        state = {"turns": 0}

        def _on_event(event_type: str, data: dict):
            if event_type == "turn_complete":
                state["turns"] += 1
            elif event_type == "wave_start":
                prefix = f"[run {idx}/{self.total_runs}] " if self.total_runs > 1 else ""
                self.say(f"{prefix}wave {data.get('wave', 0)}/{self.max_waves} "
                         f"· 턴 {state['turns']} · {', '.join(data.get('agents') or [])}")
        return _on_event


# ── run 서브커맨드 ────────────────────────────────────────────────────────────

def _dry_run(cfg, scenario_name: str) -> int:
    from backend.api.simulation.contract import preview_engine_contract
    from backend.api.simulation.schemas import ContractPreviewRequest

    print(f"# 시나리오: {scenario_name}")
    print(f"에이전트     {len(cfg.agents)}명 — {', '.join(a.name for a in cfg.agents)}")
    print(f"시작 에이전트 {cfg.start_agent}"
          f"{'  ⚠ 에이전트 목록에 없습니다' if cfg.start_agent not in {a.name for a in cfg.agents} else ''}")
    print(f"max_waves    {cfg.max_waves}")
    print(f"목표 기간     {cfg.target_duration_minutes or '미사용'}")
    print(f"시간         {cfg.time_mode} · wave당 {cfg.time_per_wave}분 · "
          f"시작 {cfg.sim_start_weekday} {cfg.sim_start_time}")
    if cfg.time_mode == "variable":
        print(f"점프 상한     동석 {cfg.max_scene_jump_minutes or '없음'}분 · "
              f"주간 {cfg.max_daytime_jump_minutes or '없음'}분")
    print(f"위치 그래프   {len(cfg.location_graph)}개 노드")
    print(f"감염 모델     {'ON — ' + (cfg.infection_model.disease_name or '(이름 없음)') if cfg.infection_model.enabled else 'OFF'}")
    print(f"디렉터       {'ON' if cfg.system_agent.enabled else 'OFF'}")
    _rel_agents = [a for a in cfg.agents if a.relationships]
    print(f"관계 지도     {f'{len(_rel_agents)}명 설정' if _rel_agents else '미사용'}")
    print(f"서버         {cfg.server_id or '(기본)'} · temperature {cfg.temperature}")
    print(f"시나리오 이벤트 {len(cfg.events)}건")

    req = ContractPreviewRequest(
        location_graph         = cfg.location_graph,
        time_mode              = cfg.time_mode,
        time_per_wave          = cfg.time_per_wave,
        infection_model        = cfg.infection_model,
        extra_fields           = cfg.extra_fields,
        output_format_override = cfg.output_format_override,
        situation_targets      = bool(cfg.location_graph),
        available_targets      = [a.name for a in cfg.agents],
        key_to_alias           = {a.name: a.display_name for a in cfg.agents if a.display_name.strip()},
    )
    try:
        preview = preview_engine_contract(req)
    except Exception as e:
        print(f"\n계약을 생성할 수 없습니다: {e}", file=sys.stderr)
        return EXIT_CONFIG
    print("\n" + "─" * 60 + "\n엔진 프롬프트 계약 (공통)\n" + "─" * 60)
    print(preview.contract)

    # 관계 지도만 에이전트마다 다르다 — 공통 계약을 N번 반복해 찍는 대신
    # 각자에게 실제로 붙는 [아는 사람] 블록만 따로 보여준다. 실존하지 않는 상대
    # key 는 실행 시 계약에서 빠지므로(Simulation._sanitize_relationships) 여기서도
    # 같은 규칙으로 표시해 dry-run 이 실행과 어긋나지 않게 한다.
    if _rel_agents:
        from ABM.prompt_contract import build_relationship_contract

        known_keys = {a.name for a in cfg.agents}
        # 엔진과 같은 2단계 역맵: {display_name: name} 을 먼저 만들고 뒤집는다.
        # display_name 이 겹치면 정방향에서 충돌해 한 명만 살아남는 것까지 실행과 일치.
        _fwd    = {a.display_name: a.name for a in cfg.agents if a.display_name.strip()}
        aliases = {v: k for k, v in _fwd.items()}
        print("\n" + "─" * 60 + "\n에이전트별 관계 지도\n" + "─" * 60)
        for a in _rel_agents:
            valid   = {k: v for k, v in a.relationships.items()
                       if k in known_keys and k != a.name}
            dangled = [k for k in a.relationships if k not in valid]
            print(f"\n## {a.name}")
            print(build_relationship_contract(valid, aliases).strip() or "  (렌더할 관계 없음)")
            for k in dangled:
                why = "자기 자신" if k == a.name else "실존하지 않는 에이전트"
                print(f"  ⚠ {k!r} — {why} (실행 시 제외됨)", file=sys.stderr)
    if preview.warnings:
        print("\n⚠ 계약 경고:", file=sys.stderr)
        for w in preview.warnings:
            print(f"  - {w}", file=sys.stderr)
    return EXIT_OK


def _write_output(md: str, path: str | None) -> None:
    if path is None or path == "-":
        sys.stdout.write(md)
        return
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)


def _finish_db_run(db, run_id: str, sim, status: str) -> None:
    """``backend/api/simulation/runner.py:finalize_run`` 의 DB 부분과 같은 기록."""
    try:
        elapsed = sim._current_elapsed_minutes(sim.completed_waves)
    except Exception:
        elapsed = getattr(sim, "_elapsed_minutes", None)
    try:
        db.finish_run(
            run_id, status, sim.completed_waves, len(sim.shared_log),
            active_agents=sim.active_agents,
            pending_wave=sim._pending_wave,
            elapsed_minutes=elapsed,
        )
    except Exception:
        pass
    # 스냅샷까지 남겨야 GUI 의 /load · /resume · 인터뷰가 CLI 실행에도 동작한다.
    try:
        snapshots = {key: agent.memory for key, agent in sim.agents.items()}
        try:
            states = sim.export_agent_state()
        except Exception:
            states = None
        db.save_agent_snapshots(run_id, snapshots, states)
    except Exception:
        pass


def cmd_run(args) -> int:
    cfg, scenario_name = _build_config(args)
    include = _parse_include(args)

    if args.dry_run:
        return _dry_run(cfg, scenario_name)

    n = max(1, args.runs)
    if n > 1 and not args.outdir:
        raise ConfigError("-n/--runs 가 2 이상이면 --outdir 이 필요합니다.")
    if args.output and args.output != "-" and n > 1:
        raise ConfigError("-o/--output 은 단일 실행에만 쓸 수 있습니다 (-n > 1 이면 --outdir).")
    if args.seed is not None and args.concurrency > 1:
        print("⚠ --seed 는 전역 random 을 건드리므로 --concurrency > 1 에서는 "
              "재현성을 보장하지 않습니다.", file=sys.stderr)

    from ABM.config import LOG_DIR
    from ABM.db import SimDB
    from ABM.export.markdown import render_markdown
    from ABM.simulation.headless import run_config
    from backend.api.simulation.runtime.llm_config import _make_agent_llm_map, _make_llm

    progress = Progress(args.quiet, n, cfg.max_waves)
    stop_ev = threading.Event()

    def _on_sigint(signum, frame):
        stop_ev.set()
        print("\n중지 신호 — 진행 중인 wave 를 마치고 종료합니다 "
              "(한 번 더 누르면 강제 종료).", file=sys.stderr)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:
        pass  # 메인 스레드가 아니면 핸들러 설치 불가 — 조용히 넘어간다

    results: list[dict] = []
    results_lock = threading.Lock()

    with LLMRuntime():
        llm       = _make_llm(cfg.server_id, cfg.temperature)
        agent_llm = _make_agent_llm_map(cfg)
        sim_db_path = os.path.join(LOG_DIR, "simulation.db")
        # SimDB 는 커넥션이 스레드 로컬이라 인스턴스 하나를 공유해도 안전하다(WAL).
        db = None if args.no_db else SimDB(sim_db_path)

        with tempfile.TemporaryDirectory(prefix="abm_cli_") as tmp_root:

            def _one(i: int) -> dict:
                idx = i + 1
                run_id = str(uuid.uuid4())
                log_dir = os.path.join(tmp_root, f"cli_run_{run_id}")
                os.makedirs(log_dir, exist_ok=True)
                if args.seed is not None:
                    random.seed(args.seed + i)
                started = time.time()
                if db is not None:
                    db.create_run(run_id, cfg.scenario_id, scenario_name, cfg.model_dump_json())
                try:
                    res = run_config(
                        cfg,
                        llm=llm,
                        agent_llm=agent_llm,
                        log_dir=log_dir,
                        db=db,
                        sim_id=run_id,
                        stop_event=stop_ev,
                        on_event=progress.callback(idx),
                    )
                except Exception as e:
                    if db is not None:
                        try:
                            db.finish_run(run_id, "error", 0, 0)
                        except Exception:
                            pass
                    progress.say(f"[run {idx}/{n}] ❌ 실행 실패: {e}")
                    return {"idx": idx, "error": e, "run_id": run_id}

                status = "stopped" if stop_ev.is_set() else "done"
                if db is not None:
                    _finish_db_run(db, run_id, res.sim, status)

                md = render_markdown(
                    config=cfg.model_dump(),
                    shared_log=res.shared_log,
                    events=res.events,
                    scenario_name=scenario_name,
                    status=status,
                    include=include,
                )
                progress.say(
                    f"[run {idx}/{n}] ✅ {res.end_reason or status} · "
                    f"wave {res.completed_waves} · 턴 {res.total_turns} · "
                    f"{time.time() - started:.0f}s"
                )
                return {
                    "idx": idx, "run_id": run_id, "md": md, "error": None,
                    "end_reason": res.end_reason,
                    "waves": res.completed_waves, "turns": res.total_turns,
                }

            if args.concurrency > 1 and n > 1:
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    for r in pool.map(_one, range(n)):
                        with results_lock:
                            results.append(r)
            else:
                for i in range(n):
                    results.append(_one(i))
                    if stop_ev.is_set():
                        break

    # ── 출력 ──────────────────────────────────────────────────────────────────
    tag = now_tag()
    base = safe_filename(scenario_name)
    for r in sorted(results, key=lambda x: x["idx"]):
        if r.get("error") is not None:
            continue
        if n > 1:
            path = os.path.join(args.outdir, f"{base}_run{r['idx']}_{tag}.md")
        elif args.output:
            path = args.output
        elif args.outdir:
            path = os.path.join(args.outdir, f"{base}_{tag}.md")
        else:
            path = f"{base}_{tag}.md"
        _write_output(r["md"], path)
        if path != "-":
            print(f"→ {path}", file=sys.stderr)

    # ── 요약 ──────────────────────────────────────────────────────────────────
    ok      = [r for r in results if r.get("error") is None]
    errored = [r for r in results if r.get("error") is not None]
    strict_hits = [r for r in ok if r.get("end_reason") in _STRICT_END_REASONS]
    if n > 1 or not args.quiet:
        by_reason: dict[str, int] = {}
        for r in ok:
            by_reason[r.get("end_reason") or "?"] = by_reason.get(r.get("end_reason") or "?", 0) + 1
        parts = " / ".join(f"{k} {v}" for k, v in sorted(by_reason.items()))
        avg_w = sum(r["waves"] for r in ok) / len(ok) if ok else 0
        avg_t = sum(r["turns"] for r in ok) / len(ok) if ok else 0
        print(f"완료 {len(ok)} / 오류 {len(errored)}"
              f"{(' — ' + parts) if parts else ''} · "
              f"평균 wave {avg_w:.1f} · 평균 턴 {avg_t:.1f}", file=sys.stderr)

    if errored:
        return EXIT_RUNTIME
    if args.strict and strict_hits:
        return EXIT_STRICT
    return EXIT_OK


# ── export 서브커맨드 ─────────────────────────────────────────────────────────

def cmd_export(args) -> int:
    from ABM.config import LOG_DIR
    from ABM.db import SimDB
    from ABM.export.markdown import render_markdown

    include = _parse_include(args)
    db = SimDB(os.path.join(LOG_DIR, "simulation.db"))

    run_id = args.run_id
    if not run_id:
        if not args.scenario_name:
            raise ConfigError("--run-id 또는 --scenario <name> 이 필요합니다.")
        runs = [r for r in db.get_runs() if (r.get("scenario_name") or "") == args.scenario_name]
        if not runs:
            raise ConfigError(f"'{args.scenario_name}' 시나리오의 실행 기록이 없습니다.")
        # get_runs() 는 started_at DESC 정렬이라 첫 항목이 최신이다.
        run_id = runs[0]["run_id"]

    run = db.get_run(run_id)
    if run is None:
        raise ConfigError(f"실행을 찾을 수 없습니다: {run_id}")
    try:
        config = json.loads(run.get("config_json") or "{}")
    except json.JSONDecodeError:
        config = {}

    md = render_markdown(
        config=config,
        shared_log=db.get_run_log(run_id),
        events=db.get_run_events(run_id),
        scenario_name=run.get("scenario_name") or "시나리오",
        status=run.get("status") or "",
        include=include,
    )
    path = args.output
    if path and path != "-" and os.path.isdir(path):
        path = os.path.join(
            path, f"{safe_filename(run.get('scenario_name') or '시나리오')}_{now_tag()}.md")
    _write_output(md, path)
    if path and path != "-":
        print(f"→ {path}", file=sys.stderr)
    return EXIT_OK


# ── 인자 파서 ─────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ABM.cli",
        description="시나리오 JSON → 시뮬레이션 → 스크린플레이 마크다운 (GUI 불필요)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def _add_toggles(sp):
        sp.add_argument("--include", metavar="a,b,c",
                        help="포함할 항목 (기본: summary 제외 전부)")
        sp.add_argument("--exclude", metavar="a,b,c", help="제외할 항목")

    # run ---------------------------------------------------------------------
    r = sub.add_parser("run", help="시나리오를 실행하고 마크다운을 만든다")
    r.add_argument("scenario", nargs="?", help="시나리오 JSON 경로")
    r.add_argument("--scenario-id", help="DB(simulation_scenarios)에 저장된 시나리오 id")
    r.add_argument("-o", "--output", help="출력 파일 (- 면 stdout)")
    r.add_argument("--outdir", help="출력 디렉토리 (-n > 1 이면 필수)")
    r.add_argument("-n", "--runs", type=int, default=1, help="반복 실행 횟수 (기본 1)")
    r.add_argument("--concurrency", type=int, default=1, help="동시 실행 수 (기본 1)")
    _add_toggles(r)
    r.add_argument("--max-waves", type=int, help="config 오버라이드")
    r.add_argument("--target-minutes", type=int, help="목표 기간(분). 0 = 미사용")
    r.add_argument("--server-id", help="LLM 서버 id 오버라이드")
    r.add_argument("--temperature", type=float, help="샘플링 온도 오버라이드")
    r.add_argument("--step-delay", type=float, help="wave 간 대기(초) 오버라이드")
    r.add_argument("--seed", type=int, help="random 시드 (N회 실행은 seed+i)")
    r.add_argument("--no-db", action="store_true", help="simulation.db 에 기록하지 않는다")
    r.add_argument("--dry-run", action="store_true",
                   help="설정 검증 + 엔진 계약 출력만 하고 실행하지 않는다")
    r.add_argument("--quiet", action="store_true", help="stderr 진행률 억제")
    r.add_argument("--strict", action="store_true",
                   help="end_reason 이 silence/no_agents 면 종료 코드 1")
    r.set_defaults(func=cmd_run)

    # export ------------------------------------------------------------------
    e = sub.add_parser("export", help="이미 끝난 실행을 마크다운으로 다시 뽑는다")
    e.add_argument("--run-id", help="simulation_runs.run_id")
    e.add_argument("--scenario", dest="scenario_name", help="시나리오 이름 (--latest 와 함께)")
    e.add_argument("--latest", action="store_true",
                   help="--scenario 의 가장 최근 실행 (기본 동작이라 생략 가능)")
    e.add_argument("-o", "--output", help="출력 파일 또는 디렉토리 (기본 stdout)")
    _add_toggles(e)
    e.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"오류: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as e:                              # noqa: BLE001 — 최상위 방어선
        import traceback
        traceback.print_exc()
        print(f"실행 오류: {e}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
