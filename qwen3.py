import requests
import json
import sys

BASE_URL = "http://172.17.3.135:8000"
MODEL = "Qwen/Qwen3.6-35B-A3B"

SYSTEM_PROMPT = "You are a helpful assistant. Respond naturally and completely without any apologies or unnecessary qualifiers."

def check_server():
    try:
        requests.get(f"{BASE_URL}/health", timeout=5).raise_for_status()
    except Exception as e:
        print(f"❌ 서버 연결 실패: {e}")
        sys.exit(1)

def stream_chat(messages: list, enable_thinking: bool = False):
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 16384,
        "temperature": 0.7,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": enable_thinking},
    }

    full_content = ""
    full_reasoning = ""
    in_thinking = False

    with requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        json=payload,
        stream=True,
    ) as r:
        r.raise_for_status()

        for line in r.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8")
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break

            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})

            # reasoning 필드 스트리밍
            reasoning_delta = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if reasoning_delta:
                if not in_thinking:
                    print("\n[Thinking]", flush=True)
                    in_thinking = True
                print(reasoning_delta, end="", flush=True)
                full_reasoning += reasoning_delta

            # content 필드 스트리밍
            content_delta = delta.get("content") or ""
            if content_delta:
                if in_thinking:
                    print("\n", flush=True)
                    in_thinking = False
                print(content_delta, end="", flush=True)
                full_content += content_delta

    print()  # 마지막 줄바꿈
    return full_content, full_reasoning


def main():
    check_server()

    print("Qwen3.6-35B-A3B 채팅 (종료: exit / thinking 토글: think)\n")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    thinking_mode = False

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n종료합니다.")
            break

        if not user_input:
            continue
        if user_input.lower() == "exit":
            print("종료합니다.")
            break
        if user_input.lower() == "think":
            thinking_mode = not thinking_mode
            print(f"[Thinking 모드: {'ON' if thinking_mode else 'OFF'}]\n")
            continue

        messages.append({"role": "user", "content": user_input})

        print("\nQwen: ", end="", flush=True)
        try:
            content, _ = stream_chat(messages, enable_thinking=thinking_mode)
            messages.append({"role": "assistant", "content": content})
        except requests.HTTPError as e:
            print(f"\nAPI 오류: {e.response.text}")
        except Exception as e:
            print(f"\n오류: {e}")

        print()

if __name__ == "__main__":
    main()