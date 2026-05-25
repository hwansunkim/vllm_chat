import requests
import json
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from .config import BASE_URL, MODEL, API_TIMEOUT

logger = logging.getLogger(__name__)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, json.JSONDecodeError, ValueError)),
)
def chat_response(
    messages: list,
    model: str = MODEL,
    base_url: str = BASE_URL,
    timeout: int = API_TIMEOUT,
) -> tuple[str, str]:
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  16384,
        "temperature": 0.7,
        "stream":      False,
    }
    r = requests.post(
        f"{base_url}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    if "choices" not in data or not data["choices"]:
        logger.error(f"응답 구조 오류: {data}")
        raise ValueError("Invalid response structure")
    message_obj = data["choices"][0].get("message", {})
    content   = message_obj.get("content", "")
    reasoning = message_obj.get("reasoning", "") or message_obj.get("reasoning_content", "")
    return content, reasoning
