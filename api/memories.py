from __future__ import annotations

from fastapi import APIRouter

from database import get_db

router = APIRouter(prefix="/api/memories")


@router.get("")
def list_memories(q: str = "", type: str = ""):
    conn = get_db()
    conditions, params = [], []
    if type:
        conditions.append("m.type = ?")
        params.append(type)
    if q:
        conditions.append(
            "(m.content LIKE ? OR m.id IN "
            "(SELECT memory_id FROM memory_keywords WHERE keyword LIKE ?))"
        )
        params.extend([f"%{q}%", f"%{q.lower()}%"])
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT m.id, m.type, m.content, m.created_at, m.last_accessed,
               GROUP_CONCAT(mk.keyword, ',') AS keywords
        FROM memories m
        LEFT JOIN memory_keywords mk ON m.id = mk.memory_id
        {where}
        GROUP BY m.id
        ORDER BY m.last_accessed DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return [
        {**dict(r), "keywords": r["keywords"].split(",") if r["keywords"] else []}
        for r in rows
    ]


@router.delete("/{memory_id}", status_code=204)
def delete_memory(memory_id: str):
    conn = get_db()
    conn.execute("DELETE FROM memory_keywords WHERE memory_id=?", (memory_id,))
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    conn.close()
