from pathlib import Path

MAX_COMPLETION_TOKENS  = 1024
MAX_CONTINUATION_ROUNDS = 5
CONTINUE_PROMPT = (
    "이전 응답이 중단되었습니다. "
    "이미 작성한 내용을 반복하지 말고 끊긴 부분부터 이어서 완성해주세요."
)
ARCHIVE_THRESHOLD    = 0.75
KEEP_RECENT_TURNS    = 4
MAX_RETRIEVED_MEMORIES = 5
DB_PATH = Path("memory.db")
