"""Episodic memory — event-based recollections per agent."""

import json
import time


class EpisodicMixin:
    def upsert_episodes(
        self,
        sim_id: str,
        agent_key: str,
        episodes: list[dict],
        wave: int,
        elapsed_minutes: int | None = None,
    ):
        """압축 배치에서 뽑아낸 사건들을 저장한다.

        `elapsed_minutes`(이 배치가 실제로 일어난 절대 경과분)를 배치의 모든
        사건에 **코드가 못박는다** — 개별 episode가 `"wave"`/`"elapsed_minutes"`를
        들고 와도 무시한다. 원문에는 wave/시각 정보가 없어(`_format_messages`는
        요일 단위 구획 헤더만 준다) LLM이 사건별 정확한 시점을 알 방법이 없고,
        묻는다고 물어도 배치 전체에 같은 값을 반복해 찍기만 했다(옛 버그). 배치
        단위 정확도(±압축 주기)면 recency 버킷 판정에는 충분하다.
        """
        conn = self._conn()
        now  = time.time()
        conn.executemany(
            "INSERT INTO episodic_memory "
            "(sim_id, agent_key, wave, elapsed_minutes, event, participants, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sim_id, agent_key, wave, elapsed_minutes,
                    ep["event"],
                    json.dumps(ep.get("participants", []), ensure_ascii=False),
                    int(ep.get("importance", 3)),
                    now,
                )
                for ep in episodes
            ],
        )
        conn.commit()

    def get_episodes(self, sim_id: str, agent_key: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT wave, elapsed_minutes, event, participants, importance "
            "FROM episodic_memory WHERE sim_id=? AND agent_key=? "
            "ORDER BY elapsed_minutes IS NULL, elapsed_minutes, id",
            (sim_id, agent_key),
        ).fetchall()
        return [dict(r) for r in rows]
