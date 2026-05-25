import json
import logging

logger = logging.getLogger(__name__)


def parse_json_response(content: str) -> tuple[str, str, str, list]:
    """LLM의 JSON 응답에서 (content, emotion, action, targets) 추출."""
    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]

        data          = json.loads(content.strip())
        clean_content = data.get("content", "")
        emotion       = data.get("emotion", "neutral")
        action        = data.get("action",  "speak")
        targets       = data.get("target",  [])

        if isinstance(targets, str):
            tl = targets.lower().strip()
            if tl == "system":
                targets = ["system"]
            elif tl == "all":
                targets = ["all"]
            else:
                targets = [t.strip() for t in targets.split(',') if t.strip()]
        elif not targets:
            targets = ["system"]

        return clean_content, emotion, action, targets

    except Exception:
        logger.warning(f"JSON 파싱 실패. 원본: {content}")
        return content, "neutral", "speak", ["system"]
