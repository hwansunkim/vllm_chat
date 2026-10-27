"""휴대폰 메시지 (phone_messages 토글) — 위치와 무관한 1:1(또는 1:N) 텍스트 채널.

출력 JSON 의 ``phone_message: {"to": [...], "text": "..."}`` 를 엔진이 처리한다(파서
`_normalize_phone_message`). 받는 사람은 보낸 사람이 **아는** 사람이어야 한다(관계 지도·만나서 알게 된 사람 =
`_agent_knowledge`). 처음 보는 사람(stranger_N)·실명을 아직 모르는 사람에게는 번호를 몰라 보낼 수 없다.

전달 규칙:
- 위치·구역·외부 공간·이동 중과 무관하게 받는 사람에게만 즉시(다음 턴 incoming) 간다. 엿듣기·접촉 없음.
- 받는 사람이 자기 선언 상태(수면·개인 용무 등)면 깨우지 않는다 — 보류됐다가 상태가 끝나면 보인다(기존
  보류 저장소 `held_incoming`). 구역 경계 이동 중(traveling)이면 도착할 때 읽는다 — 엔진은 이동 중인 사람에게
  턴을 주지 않는다(도착 전 장소에서 활동하는 서사 방지, runner 의 이동 중 강제 편입 차단 관문). 메시지 자체는
  위치와 무관하게 그 사람에게 닿아 있다(외부 공간에 있어도 받는다).
- 받은 사람은 그 메시지로 다음 wave 에 턴을 받는다(말을 걸어온 것과 같다).
- 사용은 세계 활동("휴대폰:<보낸 사람>")이다 — 단 혼자 깨어 있는 사람의 활동 규칙에서는 강한 신호가 아니다
  (잠든 가족에게 밤새 문자를 보내며 밤을 잘게 쪼개지 않게).
- 보낸 사람의 기억에는 자기 턴 JSON(phone_message 포함)이, 받은 사람의 기억에는 받은 메시지 줄이 남는다 —
  기억 압축이 둘 다 본다.
"""
from __future__ import annotations

import logging

from .status import HELD_KIND_KEY, HELD_KIND_PHONE

logger = logging.getLogger(__name__)

PHONE_TAG = "[휴대폰 메시지]"
_PHONE_MALFORMED = ('[씬] 휴대폰 메시지 형식이 잘못되었다 — "phone_message": '
                    '{"to": ["<ID>"], "text": "<보낼 내용>"} 로 써야 한다.')
_PHONE_FAIL = {
    "unknown":     "[씬] 휴대폰 메시지를 보내지 못했다 — '{to}'(이)라는 연락처가 없다.",
    "stranger":    "[씬] 휴대폰 메시지를 보내지 못했다 — {to}의 번호를 모른다.",
    "self":        "[씬] 휴대폰 메시지를 보내지 못했다 — 자기 자신에게는 보낼 수 없다.",
    "unreachable": "[씬] 휴대폰 메시지를 보내지 못했다 — {to}에게 연락이 닿지 않는다.",
}


class _PhoneMixin:
    """휴대폰 메시지 처리 — runner 의 라우팅 직후(이동 전)에 한 번 부른다."""

    def _resolve_phone_recipient(self, sender: str, raw: str) -> tuple[str | None, str | None]:
        """원본 수신자 하나 → (key, None) 또는 (None, 거절 사유)."""
        name = (raw or "").strip()
        if name.startswith("stranger_"):
            return None, "stranger"
        key = self._normalize_target(name)
        if key not in self.agents:
            return None, "unknown"
        if key == sender:
            return None, "self"
        if key not in self._agent_knowledge.get(sender, set()):
            return None, "stranger"
        if key not in self.active_agents:
            return None, "unreachable"
        return key, None

    def _phone_sender_label(self, recipient: str, sender: str) -> str:
        """받는 사람 화면의 보낸 사람 표시 — 아는 사이면 이름, 모르면 "모르는 번호"(만난 적이 없다)."""
        if sender in self._agent_knowledge.get(recipient, set()):
            return self._key_to_alias.get(sender, sender)
        return "모르는 번호"

    def _process_phone_messages(
        self,
        results:          dict,
        scene_injections: dict[str, list],
        world_activity:   set[str],
        disp_wave:        int,
        now_elapsed:      int,
    ) -> None:
        if not getattr(self, "_phone_messages", False):
            return
        for sender, result in results.items():
            if not result.get("success"):
                continue
            if result.get("phone_message_malformed"):
                scene_injections.setdefault(sender, []).append(
                    {"speaker": "씬", "action_note": "", "content": _PHONE_MALFORMED})
            msg = result.get("phone_message")
            if not msg:
                continue
            text = msg["text"]
            recipients: list[str] = []
            rejected: list[dict] = []
            for raw in msg["to"]:
                key, reason = self._resolve_phone_recipient(sender, raw)
                if key is None:
                    rejected.append({"to": raw, "reason": reason})
                    label = raw
                    if reason in ("stranger", "unreachable"):
                        real = (self._stranger_map.get(sender, {}).get(raw) if raw.startswith("stranger_")
                                else self._normalize_target(raw))
                        if real in self.agents:
                            label = (f'낯선 이(ID: "{raw}")' if raw.startswith("stranger_")
                                     else self._key_to_alias.get(real, real))
                    scene_injections.setdefault(sender, []).append({
                        "speaker": "씬", "action_note": "",
                        "content": _PHONE_FAIL[reason].format(to=label),
                    })
                    continue
                if key in recipients:
                    continue
                recipients.append(key)
                scene_injections.setdefault(key, []).append({
                    "speaker":     "씬",
                    "action_note": "",
                    "content":     f"{PHONE_TAG} {self._phone_sender_label(key, sender)}: {text}",
                    HELD_KIND_KEY: HELD_KIND_PHONE,
                })
            if recipients:
                world_activity.add(f"휴대폰:{sender}")
            held = [k for k in recipients if self._agent_self_state(k, now_elapsed) is not None]
            logger.info(f"[W{disp_wave}] 휴대폰 메시지 {sender} → {recipients} (거절 {rejected}, 보류 {held})")
            self._emit("phone_message", {
                "wave":            disp_wave,
                "sender":          sender,
                "sender_name":     self._key_to_alias.get(sender, sender),
                "recipients":      recipients,
                "recipient_names": [self._key_to_alias.get(k, k) for k in recipients],
                "text":            text,
                "rejected":        rejected,
                "held":            held,
                "elapsed_minutes": now_elapsed,
            })
