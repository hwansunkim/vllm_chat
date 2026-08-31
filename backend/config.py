import os
from pathlib import Path

VLLM_API_KEY      = os.environ.get("VLLM_API_KEY", "")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MAX_COMPLETION_TOKENS         = 4096
MAX_COMPLETION_TOKENS_THINKING = 16384
MAX_CONTINUATION_ROUNDS = 5

# ── 사고(thinking) 수준 ────────────────────────────────────────────────
# 프로바이더 중립 4단계. 백엔드가 벤더별 파라미터로 번역한다.
#   vLLM      : off 외에는 chat_template_kwargs.enable_thinking=true
#               (+ reasoning_effort 를 함께 넣어 세부 강도 지원 모델에 위임)
#   OpenAI    : 추론 모델(gpt-5/o1/o3/o4)에만 reasoning_effort=<level>
#   Anthropic : thinking.budget_tokens 를 아래 표로 결정
THINKING_LEVELS = ("off", "low", "medium", "high")

# Anthropic extended thinking 의 budget_tokens.
THINKING_BUDGET_BY_LEVEL = {"low": 2048, "medium": 8192, "high": 24576}

# 하위호환 별칭. 구 단일 상수(10000)를 참조하던 코드가 medium 을 가리키게 한다.
THINKING_BUDGET_TOKENS = THINKING_BUDGET_BY_LEVEL["medium"]


def normalize_thinking_level(value, default: str = "off"):
    """bool / str / None 을 정규 thinking level 문자열로 변환한다.

    - None                → `default` 를 그대로 반환 (호출자가 "미지정"을 구분할 수
                            있도록 default=None 도 허용한다)
    - bool/int            → 참→"medium", 거짓→"off" (구 `thinking` 컬럼/필드 호환)
                            SQLite 의 INTEGER 컬럼은 Python `int` 로 돌아오고
                            `isinstance(1, bool)` 은 False 이므로 int 도 함께 받아야
                            DB 행 폴백이 실제로 동작한다.
    - 유효한 level 문자열 → 소문자로 정규화해 그대로
    - 그 외(오타/구버전)  → `default`
    """
    if value is None:
        return default
    if isinstance(value, (bool, int)):
        return "medium" if value else "off"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in THINKING_LEVELS:
            return v
    return default



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
