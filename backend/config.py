import os
from pathlib import Path

VLLM_API_KEY      = os.environ.get("VLLM_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_COMPLETION_TOKENS         = 4096
MAX_COMPLETION_TOKENS_THINKING = 16384
MAX_CONTINUATION_ROUNDS = 5
CONTINUE_PROMPT = "계속"
CONTINUE_PROMPT_THINKING = "계속"
ARCHIVE_THRESHOLD    = 0.75
KEEP_RECENT_TURNS    = 4
MAX_RETRIEVED_MEMORIES = 5
DB_PATH = Path("memory.db")

# ── 웹 검색 ────────────────────────────────────────────────────────────
# 트리거 방식. 현재는 "toggle" (프론트 웹검색 버튼)만 구현되어 있다.
#   향후 "tool_calling" 으로 교체하려면:
#     1) LLM 요청에 search 함수 정의를 tools= 로 전달
#     2) 응답의 tool_call 을 받아 backend/websearch/service.web_search() 를 그대로 호출
#     3) 결과를 tool 메시지로 넣고 재요청 (에이전트 루프)
#   → 검색 실행/결과 포맷 레이어(websearch/)는 재사용, conversations.send_chat 의
#     오케스트레이션 부분만 교체하면 된다.
WEB_SEARCH_MODE = os.environ.get("WEB_SEARCH_MODE", "toggle")  # "toggle" | "tool_calling"(미구현)

# 검색 백엔드. 현재는 "duckduckgo" (HTML 파싱, 키 불필요)만 구현.
#   "tavily" / "searxng" 등을 쓰려면 backend/websearch/providers/ 에 어댑터를
#   추가하고 service.get_provider() 에 분기를 넣은 뒤 이 값만 바꾸면 된다.
WEB_SEARCH_PROVIDER = os.environ.get("WEB_SEARCH_PROVIDER", "duckduckgo")

WEB_SEARCH_MAX_RESULTS       = int(os.environ.get("WEB_SEARCH_MAX_RESULTS", "5"))
WEB_SEARCH_TIMEOUT           = float(os.environ.get("WEB_SEARCH_TIMEOUT", "10"))
WEB_SEARCH_MAX_SNIPPET_CHARS = 500
WEB_SEARCH_REWRITE_QUERY     = os.environ.get("WEB_SEARCH_REWRITE_QUERY", "1") == "1"
