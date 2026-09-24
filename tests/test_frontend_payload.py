"""프론트엔드 설정 직렬화기(frontend/js/sim/config.js) ↔ SimStartConfig 대조.

배경: 저장 경로(scenarios.js)와 실행 경로(run/control.js)가 필드 목록을 각자 나열하던
시절, /start 요청에서만 state_categories·zone_travel_* 가 빠져 UI에서 추가한 상태와
사용자 설정 시간이 조용히 백엔드 기본값으로 대체됐다. 지금은 두 경로가 모두
`buildSimConfig()` 를 쓰며, 이 테스트가 그 계약을 정적으로 지킨다.

- 스키마에 있는데 빌더에 없는 필드 → 실패 (새 필드를 추가하고 프론트를 잊은 경우)
- 빌더에만 있는 키 → 실패 (오타 — pydantic 이 조용히 무시한다)
- /start 요청·시나리오 저장·파일 내보내기가 실제로 buildSimConfig() 를 쓰는지 확인
"""
import re
import unittest
from pathlib import Path

from backend.api.simulation.schemas import SimStartConfig

ROOT = Path(__file__).resolve().parent.parent
SIM_JS = ROOT / "frontend" / "js" / "sim"
CONFIG_JS = SIM_JS / "config.js"
CONTROL_JS = SIM_JS / "run" / "control.js"
SCENARIOS_JS = SIM_JS / "scenarios.js"

# 빌더가 의도적으로 내보내지 않는 SimStartConfig 필드와 그 이유.
INTENTIONALLY_EXCLUDED = {
    # 실행 전용 — 시나리오 저장본 config 에는 넣지 않고 startSimulation() 이
    # `{scenario_id, ...buildSimConfig()}` 로 붙인다(아래 테스트가 확인).
    "scenario_id": "실행 요청에서만 붙는 키",
    # 구 필드. 백엔드가 불러올 때 output_format_override 로 옮긴다
    # (backend/api/simulation/scenarios.py). 프론트는 새로 보내지 않는다.
    "output_format_template": "구 필드 — override 로 이관됨",
}


def _strip_line_comment(line: str) -> str:
    """문자열 리터럴 밖의 `//` 주석을 떼어낸다."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif line.startswith("//", i):
            return line[:i]
        i += 1
    return line


def _function_return_keys(src: str, func_name: str) -> list[str]:
    """`function <func_name>(...) { return { ... }; }` 의 최상위 키 목록."""
    m = re.search(rf"function\s+{func_name}\s*\([^)]*\)\s*\{{\s*return\s*\{{", src)
    assert m, f"{func_name}() 의 `return {{` 를 찾지 못했다"
    keys: list[str] = []
    depth = 0
    for raw in src[m.end():].splitlines():
        line = _strip_line_comment(raw)
        if depth == 0:
            km = re.match(r"\s*([A-Za-z_$][\w$]*)\s*:", line)
            if km:
                keys.append(km.group(1))
            if re.match(r"\s*\}", line):
                return keys          # return 객체의 닫는 중괄호
        # 문자열 안의 괄호는 세지 않는다
        code = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`", "''", line)
        depth += sum(code.count(c) for c in "([{") - sum(code.count(c) for c in ")]}")
    raise AssertionError(f"{func_name}() return 객체의 끝을 찾지 못했다")


def _builder_keys() -> list[str]:
    return _function_return_keys(CONFIG_JS.read_text(encoding="utf-8"), "buildSimConfig")


class FrontendSimConfigPayloadTests(unittest.TestCase):
    def test_parser_sees_known_keys(self):
        keys = _builder_keys()
        # 파서 자체 점검 — 너무 적게 잡히면 아래 대조가 무의미해진다.
        for k in ("agents", "state_categories", "zone_travel_min_minutes",
                  "zone_travel_max_minutes", "infection_model"):
            self.assertIn(k, keys)
        self.assertEqual(len(keys), len(set(keys)), f"중복 키: {keys}")

    def test_builder_covers_every_schema_field(self):
        schema = set(SimStartConfig.model_fields)
        missing = schema - set(_builder_keys()) - set(INTENTIONALLY_EXCLUDED)
        self.assertFalse(
            missing,
            f"SimStartConfig 에는 있는데 buildSimConfig()(frontend/js/sim/config.js)가 "
            f"보내지 않는 필드: {sorted(missing)} — 빌더에 추가하거나 "
            f"INTENTIONALLY_EXCLUDED 에 이유와 함께 등록하라.",
        )

    def test_builder_has_no_unknown_keys(self):
        extra = set(_builder_keys()) - set(SimStartConfig.model_fields)
        self.assertFalse(extra, f"스키마에 없는 키(오타?): {sorted(extra)}")

    def test_excluded_fields_still_exist_in_schema(self):
        # 스키마에서 사라진 필드를 제외 목록에 남겨 두지 않도록.
        stale = set(INTENTIONALLY_EXCLUDED) - set(SimStartConfig.model_fields)
        self.assertFalse(stale, f"제외 목록에만 남은 필드: {sorted(stale)}")
        # 의도적 제외 필드는 빌더에 실제로 없어야 한다.
        self.assertFalse(set(INTENTIONALLY_EXCLUDED) & set(_builder_keys()))

    def test_start_request_uses_shared_builder(self):
        src = CONTROL_JS.read_text(encoding="utf-8")
        self.assertRegex(src, r"import\s*\{[^}]*\bbuildSimConfig\b[^}]*\}\s*from\s*'\.\./config\.js'")
        m = re.search(r"fetch\('/api/simulation/start',(.*?)\n  \}\);", src, re.S)
        self.assertIsNotNone(m, "/api/simulation/start fetch 를 찾지 못했다")
        call = m.group(1)
        self.assertIn("...buildSimConfig()", call)
        # 스프레드 외에 필드를 따로 나열하면 다시 두 목록이 갈라진다 — scenario_id 만 허용.
        body = re.search(r"JSON\.stringify\(\{(.*?)\}\)", call, re.S).group(1)
        literal_keys = {
            km.group(1)
            for line in body.splitlines()
            if (km := re.match(r"\s*([A-Za-z_]\w*)\s*:", _strip_line_comment(line)))
        }
        self.assertEqual(literal_keys, {"scenario_id"})

    def test_scenario_save_and_export_use_shared_builder(self):
        src = SCENARIOS_JS.read_text(encoding="utf-8")
        self.assertRegex(src, r"import\s*\{[^}]*\bbuildSimConfig\b[^}]*\}\s*from\s*'\./config\.js'")
        self.assertIn("config: buildSimConfig(),", src)              # saveScenario
        self.assertIn("const config = buildSimConfig();", src)       # exportScenarioFile
        # 옛 개별 빌더가 되살아나지 않도록.
        self.assertNotRegex(src, r"function\s+buildScenarioConfig\b")

    def test_builder_default_policies(self):
        src = CONFIG_JS.read_text(encoding="utf-8")
        # [] 는 유효값(상태 기능 off) — `||` 로 쓰면 undefined 가 조용히 [] 로 바뀌어 기능이 꺼진다.
        self.assertRegex(src, r"state_categories:\s*Array\.isArray\(s\.state_categories\)")
        # 이동 시간 0 은 유효값 — `??`.
        self.assertRegex(src, r"zone_travel_min_minutes:\s*s\.zone_travel_min_minutes\s*\?\?")
        self.assertRegex(src, r"zone_travel_max_minutes:\s*s\.zone_travel_max_minutes\s*\?\?")


if __name__ == "__main__":
    unittest.main()
