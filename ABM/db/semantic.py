"""Semantic memory — facts/beliefs with confidence tracking.

Only meaningfully different confidence levels overwrite existing facts; small
fluctuations are ignored to avoid log churn. Contradicting evidence blends
confidence downward rather than flipping the fact.

**ID 기반 매칭 (기억 메커니즘 2~3단계, 2026-10).** 압축 LLM 은 후보 사실(id 포함)을 보고
각 새 사실에 `judgment`(신규·동일_의미·변경·보류)와 `matches_id`를 단다
(``ABM/memory_compressor.py``). 예전엔 문장이 **완전히 같을 때만** 기존 행과 합쳐져서,
날짜만 바뀐 같은 사실("6일차 …까지 맛과 냄새를 못 느낀다" / "7일차 …")이 행을 계속
늘렸다(실측: 한 에이전트에 같은 사실 6개 이상, 전부 확신 100%).

``matches_id`` 가 받아들여지는 조건 — **둘 다** 만족해야 한다:
  1. 같은 `sim_id` + `agent_key` 의 행이다(남의 기억·다른 실행의 기억을 덮지 않게).
  2. 이번 압축 호출에서 LLM 에게 **실제로 후보로 보여준 id**다(`candidate_ids`). 보지도
     못한 기억을 LLM 이 지어낸 id 로 덮지 않게(코드 리뷰 1번).
  `candidate_ids=None`(직접 호출·구 경로)이면 **ID 매칭 자체를 끈다** — 누가 무엇을 보고
  판정했는지 알 수 없으므로 안전 쪽으로 기울어, 모든 항목이 문자열 매칭 경로를 탄다.
  조건을 못 넘으면 신규로 폴백한다(기존 행은 건드리지 않음).

라우팅:
  - 동일_의미 → 새 행 없음. 그 행의 `source_message_ids`에 이번 배치 근거만 누적한다.
    **확신도·updated_at·elapsed_minutes 는 건드리지 않는다** — 같은 사실의 재진술을 새
    발생으로 치지 않는다. "진짜 반복 강화"(확신 상향)는 별도 결정이다.
  - 변경 → 바뀌기 직전 버전(문장·확신도·근거·시각)을 `semantic_memory_history`에 남기고
    (reason='changed') 행을 새 문장·확신도로 갱신한다. 현재 행의 `source_message_ids`는
    **이번 배치로 리셋**한다 — 이전 근거는 반대/이전 내용을 지지했던 것이라 현재 문장의
    지지 횟수(2차 정리의 "K번의 대화에서 확인")에 섞이면 안 된다(리뷰 2번). 이전 근거는
    이력 쪽에 그대로 있다. `prev_fact`/`prev_confidence`는 하위 호환으로 직전 값을 계속 둔다. 문장이 그대로인
    "변경"은 내용 변화가 아니므로 **동일_의미와 똑같이** 처리한다(근거만 추가, 확신도·시각·prev
    불변, 이력 없음).
    A→B→C 뒤에도 A·B 를 `get_fact_history()`로 조회할 수 있다(리뷰 3번 — 원문 근거가 NULL 인
    옛 행도 최초 구조화 문장이 이력에 남는다).
  - 변경 결과 문장이 같은 sim/agent 의 **다른 행**과 같으면(리뷰 4번) 대표 하나로 통합한다:
    변경 대상 행(LLM 이 지칭한 id)이 대표가 되고, 다른 행은 이력에 reason='merged',
    merged_into=대표 로 보존한 뒤 `semantic_memory`에서 지운다. 그 행의 근거는 현재 문장을
    실제로 지지했으므로 대표 행 근거에 합치고, 확신도는 둘 중 **큰 값**(max)이다. 이번 호출에서
    지운 행 id 를 같은 배치에서 다시 지칭하면 대표 행으로 이어진다. 예전 호출에서 지운 행이면
    이력의 merged_into 를 따르되, 그 대표 행도 이번 후보 집합에 있어야 받아들인다.
  - 쓰기 도중 실패하면 이번 호출 전체를 롤백한다(이력·삭제·갱신이 반쯤 남지 않게).
  - 신규·보류·matches_id 없음·검증 실패 → 예전과 똑같은 문자열 매칭 경로(새 행 INSERT).
"""

import json
import sqlite3
import time

# 압축 LLM 판정값 — memory_compressor._normalize_fact_judgment 와 같은 집합.
JUDGMENT_NEW, JUDGMENT_SAME, JUDGMENT_CHANGED, JUDGMENT_UNSURE = "신규", "동일_의미", "변경", "보류"


def _merge_source_ids(existing_json, new_ids) -> str | None:
    """`source_message_ids` JSON 에 새 근거 id 를 순서 유지·중복 없이 덧붙인다.

    둘 다 비면 기존 값 그대로(None 이면 None) — 근거를 모르는 옛 행에 빈 배열을 억지로
    채우지 않는다.
    """
    try:
        cur = json.loads(existing_json) if existing_json else []
        if not isinstance(cur, list):
            cur = []
    except (TypeError, ValueError):
        cur = []
    add = [int(i) for i in (new_ids or [])]
    if not add:
        return existing_json
    seen = set(cur)
    merged = list(cur) + [i for i in add if not (i in seen or seen.add(i))]
    return json.dumps(merged)

# Confidence delta required to overwrite an existing fact with new evidence.
# 0.15: meaningful shift (e.g. 0.6->0.75 updates, 0.8->0.99 updates, 0.8->0.75 keeps)
CONFIDENCE_UPDATE_THRESHOLD = 0.15


class SemanticMixin:
    def upsert_facts(
        self,
        sim_id: str,
        agent_key: str,
        facts: list[dict],
        wave: int,
        elapsed_minutes: int | None = None,
        source_message_ids: list[int] | None = None,
        candidate_ids=None,
    ):
        """사실을 반영한다. 라우팅·검증은 모듈 docstring 참고.

        `source_message_ids` 는 이번 압축 배치의 원문 메시지 id(`save_messages` 반환값)로,
        이 배치에서 나온 모든 사실의 근거로 기록된다(배치 단위 — 문장별 근거는 알 수 없다).
        `candidate_ids` 는 이번 압축 호출에서 LLM 에게 보여준 후보 사실 id 들 — None 이면 ID
        매칭(동일_의미·변경)을 쓰지 않는다.
        """
        conn = self._conn()
        now  = time.time()
        batch_src = [int(i) for i in (source_message_ids or [])]
        batch_json = json.dumps(batch_src) if batch_src else None
        shown = None if candidate_ids is None else {int(i) for i in candidate_ids}

        def _fresh(row_id: int) -> sqlite3.Row | None:
            # 갱신 직후 행을 다시 읽는다 — 같은 배치 안에서 같은 문장을 가리키는 다음 항목이
            # 갱신 **전** 스냅샷과 비교하면 prev_fact 가 날아간다(QA 재현).
            return conn.execute(
                "SELECT id, fact, confidence, source_message_ids FROM semantic_memory WHERE id=?",
                (row_id,),
            ).fetchone()

        # 이번 호출에서 통합으로 지운 행 → 대표 행. 같은 배치의 후속 항목이 지운 id 를 다시
        # 지칭하면 이 대표로 잇는다(이번 호출이 직접 만든 리다이렉트라 후보 밖이어도 안전).
        merged_now: dict[int, int] = {}

        def _target(mid: int) -> sqlite3.Row | None:
            """matches_id → 지금 살아 있는 행(같은 sim/agent). 통합으로 지워진 행이면 대표로
            잇는다 — 이번 호출에서 지운 행이면 그 대표, 예전 호출에서 지운 행이면 이력의
            merged_into 를 따르되 **그 대표도 이번 후보 집합에 있어야** 받아들인다(보지 못한
            기억을 리다이렉트로 우회해 고치지 않게)."""
            seen: set[int] = set()
            cur, via_history = mid, False
            while cur not in seen:
                seen.add(cur)
                row = conn.execute(
                    "SELECT id, fact, confidence, source_message_ids, source_wave, "
                    "elapsed_minutes, updated_at FROM semantic_memory "
                    "WHERE id=? AND sim_id=? AND agent_key=?",
                    (cur, sim_id, agent_key),
                ).fetchone()
                if row is not None:
                    if via_history and row["id"] not in shown:
                        return None
                    return row
                if cur in merged_now:
                    cur = merged_now[cur]
                    continue
                nxt = conn.execute(
                    "SELECT merged_into FROM semantic_memory_history "
                    "WHERE fact_id=? AND sim_id=? AND agent_key=? AND reason='merged' "
                    "ORDER BY id DESC LIMIT 1",
                    (cur, sim_id, agent_key),
                ).fetchone()
                if nxt is None or nxt[0] is None:
                    return None
                cur, via_history = int(nxt[0]), True
            return None

        def _remap(text: str, gone_id: int) -> None:
            """`text` 문장의 검색 사전 항목을 정리한다 — 같은 문장의 다른 살아 있는 행이 있으면
            그 행으로 다시 잇고(기존 중복 행이 같은 옛 문장을 공유하는 경우), 없으면 뺀다."""
            alt = conn.execute(
                "SELECT id, fact, confidence, source_message_ids FROM semantic_memory "
                "WHERE sim_id=? AND agent_key=? AND fact=? AND id<>? ORDER BY id LIMIT 1",
                (sim_id, agent_key, text, gone_id),
            ).fetchone()
            if alt is not None:
                existing[text] = alt
            else:
                existing.pop(text, None)

        def _archive(row, reason: str, merged_into: int | None = None) -> None:
            conn.execute(
                "INSERT INTO semantic_memory_history "
                "(fact_id, sim_id, agent_key, fact, confidence, source_message_ids, source_wave, "
                " elapsed_minutes, updated_at, superseded_at, reason, merged_into) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["id"], sim_id, agent_key, row["fact"], float(row["confidence"]),
                 row["source_message_ids"], row["source_wave"], row["elapsed_minutes"],
                 row["updated_at"], now, reason, merged_into),
            )

        existing: dict[str, sqlite3.Row] = {
            r["fact"]: r
            for r in conn.execute(
                "SELECT id, fact, confidence, source_message_ids FROM semantic_memory "
                "WHERE sim_id=? AND agent_key=?",
                (sim_id, agent_key),
            ).fetchall()
        }

        # 실패하면 이번 호출의 쓰기 전체를 되돌린다 — 커밋도 롤백도 안 된 트랜잭션이 남으면
        # 다른 커넥션의 쓰기가 막히고, 같은 스레드의 다음 무관한 commit 이 반쯤 적용된
        # 이력·삭제·갱신을 확정해 버린다(QA 재현: confidence=None → float() TypeError).
        try:
            self._upsert_facts_body(conn, sim_id, agent_key, facts, wave, elapsed_minutes,
                                    batch_src, batch_json, shown, now, existing,
                                    _fresh, _target, _remap, _archive, merged_now)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _upsert_facts_body(self, conn, sim_id, agent_key, facts, wave, elapsed_minutes,
                           batch_src, batch_json, shown, now, existing,
                           _fresh, _target, _remap, _archive, merged_now) -> None:
        """`upsert_facts`의 실제 반영 루프(트랜잭션 경계는 호출부가 쥔다)."""
        for f in facts:
            new_fact  = f["fact"]
            new_conf  = float(f.get("confidence", 1.0))
            prev_fact = f.get("prev_fact")
            prev_conf = f.get("prev_confidence")

            judgment = f.get("judgment")
            mid      = f.get("matches_id")
            if (shown is not None and judgment in (JUDGMENT_SAME, JUDGMENT_CHANGED)
                    and isinstance(mid, int) and not isinstance(mid, bool) and mid in shown):
                target = _target(mid)
                if target is not None:
                    # 문장이 그대로인 "변경"은 내용 변화가 아니다 — 동일_의미와 **완전히 같은**
                    # 경로(근거만 추가, 확신도·시각·prev 불변, 이력 없음). 예전엔 확신도 등을
                    # 덮어써서 0.95→0.2 같은 변화가 이력 없이 남아 복원할 수 없었다(QA).
                    if judgment == JUDGMENT_SAME or target["fact"] == new_fact:
                        # 재진술 — 근거만 누적. 확신도·시각은 그대로(모듈 docstring).
                        conn.execute(
                            "UPDATE semantic_memory SET source_message_ids=? WHERE id=?",
                            (_merge_source_ids(target["source_message_ids"], batch_src),
                             target["id"]),
                        )
                        existing[target["fact"]] = _fresh(target["id"])
                        continue

                    # ── 변경 ── 같은 문장이 이미 다른 행에 있으면 대표(target)로 통합한다.
                    dup = conn.execute(
                        "SELECT id, fact, confidence, source_message_ids, source_wave, "
                        "elapsed_minutes, updated_at FROM semantic_memory "
                        "WHERE sim_id=? AND agent_key=? AND fact=? AND id<>?",
                        (sim_id, agent_key, new_fact, target["id"]),
                    ).fetchall()
                    src_json = batch_json      # 현재 근거는 이번 배치로 리셋(리뷰 2번)
                    conf = new_conf
                    for d in dup:
                        _archive(d, "merged", merged_into=target["id"])
                        conn.execute("DELETE FROM semantic_memory WHERE id=?", (d["id"],))
                        merged_now[int(d["id"])] = int(target["id"])
                        # 그 행의 근거는 지금 문장을 실제로 지지했다 → 대표 근거에 합친다.
                        src_json = _merge_source_ids(d["source_message_ids"],
                                                     json.loads(src_json) if src_json else [])
                        conf = max(conf, float(d["confidence"]))
                    _archive(target, "changed")
                    # 현재 근거는 이번 배치(+통합된 같은 문장 행의 근거)로 리셋 — 이전 버전의
                    # 근거는 이력에 있다(리뷰 2번).
                    conn.execute(
                        "UPDATE semantic_memory "
                        "SET fact=?, confidence=?, source_wave=?, elapsed_minutes=?, "
                        "prev_fact=?, prev_confidence=?, updated_at=?, source_message_ids=? "
                        "WHERE id=?",
                        (new_fact, conf, wave, elapsed_minutes,
                         target["fact"], float(target["confidence"]), now, src_json,
                         target["id"]),
                    )
                    _remap(target["fact"], target["id"])     # 옛 문장을 공유하던 다른 행이 있으면 그쪽으로
                    existing[new_fact] = _fresh(target["id"])
                    continue
                # target 없음(다른 에이전트/다른 실행/없는 id) → 아래 기존 경로로 폴백.

            if new_fact in existing:
                old      = existing[new_fact]
                old_conf = float(old["confidence"])
                merged   = _merge_source_ids(old["source_message_ids"], batch_src)
                # Update only if new evidence is meaningfully stronger,
                # or the fact itself changed (prev_fact provided).
                if new_conf > old_conf + CONFIDENCE_UPDATE_THRESHOLD or prev_fact:
                    conn.execute(
                        "UPDATE semantic_memory "
                        "SET fact=?, confidence=?, source_wave=?, elapsed_minutes=?, "
                        "prev_fact=?, prev_confidence=?, updated_at=?, source_message_ids=? "
                        "WHERE id=?",
                        (new_fact, new_conf, wave, elapsed_minutes,
                         prev_fact, prev_conf, now, merged, old["id"]),
                    )
                elif new_conf < old_conf - CONFIDENCE_UPDATE_THRESHOLD:
                    # Contradicting evidence — lower confidence, note uncertainty.
                    blended = (old_conf + new_conf) / 2
                    conn.execute(
                        "UPDATE semantic_memory SET confidence=?, updated_at=?, "
                        "source_message_ids=? WHERE id=?",
                        (blended, now, merged, old["id"]),
                    )
                else:
                    # small difference — keep existing entry unchanged (근거만 누적).
                    conn.execute(
                        "UPDATE semantic_memory SET source_message_ids=? WHERE id=?",
                        (merged, old["id"]),
                    )
                existing[new_fact] = _fresh(old["id"])
            else:
                conn.execute(
                    "INSERT INTO semantic_memory "
                    "(sim_id, agent_key, fact, confidence, source_wave, elapsed_minutes, "
                    "prev_fact, prev_confidence, updated_at, source_message_ids) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sim_id, agent_key, new_fact, new_conf, wave, elapsed_minutes,
                     prev_fact, prev_conf, now, batch_json),
                )

    def get_fact_history(self, sim_id: str, agent_key: str, fact_id: int) -> list[dict]:
        """한 사실의 모든 버전 — 오래된 것부터, 마지막은 현재 행(`reason='current'`).

        과거 버전은 `semantic_memory_history`에서 온다. 이 사실(대표)로 **통합된** 다른
        행들(reason='merged', merged_into=fact_id)과 그 행들의 이전 버전도 함께 돌려준다 —
        각 항목의 `fact_id`로 어느 행의 버전인지 구분한다. 대체된 시각 순(같으면 이력 id 순)이다.
        현재 행이 없으면(다른 행으로 통합돼 지워졌으면) 과거 버전만 돌려준다.
        """
        conn = self._conn()
        ids = {int(fact_id)}
        frontier = [int(fact_id)]
        while frontier:      # 이 행으로 통합된 행들을 재귀적으로 모은다
            nxt = []
            for fid in frontier:
                for (mid,) in conn.execute(
                    "SELECT DISTINCT fact_id FROM semantic_memory_history "
                    "WHERE sim_id=? AND agent_key=? AND reason='merged' AND merged_into=?",
                    (sim_id, agent_key, fid),
                ).fetchall():
                    if mid not in ids:
                        ids.add(mid)
                        nxt.append(mid)
            frontier = nxt
        rows = conn.execute(
            f"SELECT fact_id, fact, confidence, source_message_ids, source_wave, elapsed_minutes, "
            f"updated_at, superseded_at, reason, merged_into FROM semantic_memory_history "
            f"WHERE sim_id=? AND agent_key=? AND fact_id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY superseded_at, id",
            (sim_id, agent_key, *sorted(ids)),
        ).fetchall()
        out = [dict(r) for r in rows]
        cur = conn.execute(
            "SELECT id AS fact_id, fact, confidence, source_message_ids, source_wave, "
            "elapsed_minutes, updated_at FROM semantic_memory "
            "WHERE id=? AND sim_id=? AND agent_key=?",
            (int(fact_id), sim_id, agent_key),
        ).fetchone()
        if cur is not None:
            out.append({**dict(cur), "superseded_at": None, "reason": "current", "merged_into": None})
        return out

    def get_facts(self, sim_id: str, agent_key: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT fact, confidence FROM semantic_memory "
            "WHERE sim_id=? AND agent_key=? ORDER BY confidence DESC, updated_at DESC",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_facts(self, sim_id: str, agent_key: str) -> list[dict]:
        """id·`source_message_ids` 포함 전체 사실 — `get_facts()`와 달리 상한 없이, id 순.

        1차 압축의 후보 검색(`ABM/memory_compressor.py::_select_candidate_facts` — "기존
        기억" 재진술에 id 를 붙여 보여준다)과 2차 기억 정리(`consolidate_facts`)가 쓴다.
        실행 중 프롬프트 주입(`build_memory_block`)에는 확신순·표시 상한이 있는
        `get_facts()`(및 `_fact_lines`)를 쓸 것.
        """
        rows = self._conn().execute(
            "SELECT id, fact, confidence, source_message_ids FROM semantic_memory "
            "WHERE sim_id=? AND agent_key=? ORDER BY id",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_source_batches(self, source_message_ids_json) -> int | None:
        """사실의 원문 근거가 몇 번의 **압축 배치**(= 따로 확인된 대화)에서 왔는가.

        한 배치의 메시지는 같은 `created_at`으로 저장되므로(`save_messages`) 근거 id 들의
        서로 다른 `created_at` 수가 배치 수다 — 메시지 수를 그대로 세면 한 배치에서 딱 한 번
        나온 사실도 "근거 30건"처럼 보여 2차 정리의 쇠퇴 판정을 왜곡한다. 근거가 없는 옛 행
        (NULL)·빈 배열·파싱 불가면 None(모름).
        """
        try:
            ids = json.loads(source_message_ids_json) if source_message_ids_json else None
        except (TypeError, ValueError):
            return None
        if not isinstance(ids, list) or not ids:
            return None
        ids = [int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)]
        if not ids:
            return None
        conn = self._conn()
        rows = []
        # SQLite 변수 상한을 피해 나눠 조회하되, 청크 경계에 걸친 배치를 두 번 세지 않도록
        # created_at 을 모두 모은 뒤 한 번에 센다.
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows += conn.execute(
                f"SELECT created_at FROM messages WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
        return len({r[0] for r in rows}) or None

    def update_fact_confidence(self, fact_id: int, confidence: float) -> None:
        """2차 기억 정리가 사실의 확신도만 재평가한다 — 행 자체는 지우지 않는다.

        (강화/쇠퇴 둘 다 이 메서드 하나로 처리 — 방향은 호출부가 정한다.)
        """
        conn = self._conn()
        conn.execute(
            "UPDATE semantic_memory SET confidence=?, updated_at=? WHERE id=?",
            (confidence, time.time(), fact_id),
        )
        conn.commit()
