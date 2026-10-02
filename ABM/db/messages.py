"""Raw message audit-trail storage."""

import time


class MessagesMixin:
    def save_messages(
        self,
        sim_id: str,
        agent_key: str,
        messages: list[dict],
        wave: int,
    ) -> list[int]:
        """원문 메시지를 보관하고 **삽입된 행 id 목록**(입력 순서)을 돌려준다.

        id 는 압축으로 뽑힌 사실의 원문 근거(`semantic_memory.source_message_ids`)로
        쓰인다(기억 메커니즘 2~3단계). 행마다 `lastrowid` 를 받으려고 `executemany`
        대신 한 줄씩 넣는다 — 한 배치는 수십 행이라 비용 차이는 무시할 만하다.
        """
        conn = self._conn()
        now  = time.time()
        ids: list[int] = []
        # 중간 행이 실패하면(예: content 가 None) 앞서 넣은 행까지 되돌린다 — 커밋도 롤백도
        # 안 된 채 남으면 열린 쓰기 트랜잭션이 다른 스레드의 쓰기를 `database is locked`로
        # 막는다(QA 재현). 긴 트랜잭션으로 잠금을 잡지 않는다는 설계(`delete_messages`)와 같다.
        try:
            for m in messages:
                cur = conn.execute(
                    "INSERT INTO messages (sim_id, agent_key, role, content, wave, token_est, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        sim_id, agent_key,
                        m["role"], m["content"], wave,
                        max(1, len(m["content"].encode("utf-8")) // 4),
                        now,
                    ),
                )
                ids.append(int(cur.lastrowid))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return ids

    def delete_messages(self, ids: list[int]) -> None:
        """`save_messages` 로 넣은 행을 되돌린다 — 압축 LLM 호출이 실패했을 때 전용.

        `compress()` 는 근거 id 를 미리 확보하려고 LLM 호출 **전**에 원문을 저장한다.
        호출(또는 응답 파싱)이 실패하면 예전처럼 "실패한 배치는 보관하지 않는다"는
        보장을 지키려고 방금 넣은 행만 정확히 지운다. 트랜잭션을 LLM 호출 동안 열어
        두는 방식은 쓰지 않는다 — 긴 호출 동안 쓰기 잠금을 잡아 병렬로 압축하는 다른
        에이전트의 쓰기가 막힐 수 있기 때문이다.
        """
        if not ids:
            return
        conn = self._conn()
        conn.executemany("DELETE FROM messages WHERE id=?", [(int(i),) for i in ids])
        conn.commit()
