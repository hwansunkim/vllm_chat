import requests
import json
import sys
import time
import os
import re
import logging
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- 설정 ---
BASE_URL = "http://172.17.3.135:8000"
MODEL = "Qwen/Qwen3.6-35B-A3B"
LOG_DIR = "logs_graph"

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- 에이전트 클래스 ---
class Agent:
    def __init__(self, name, system_prompt, initial_memory, position):
        self.name = name
        self.system_prompt = system_prompt
        self.log_file = os.path.join(LOG_DIR, f"{name}.json")
        self.memory = initial_memory
        self.position = position
        self.init_log_file()

    def init_log_file(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)

    def add_to_log(self, message, reasoning="", emotion="", action="", targets=None):
        if targets is None:
            targets = []
            
        log_entry = {
            "timestamp": time.time(),
            "datetime_str": datetime.now().isoformat(),
            "role": message['role'],
            "content": message['content'],
            "clean_content": message.get('clean_content', ''),
            "action_note": message.get('action_note', ''),
            "reasoning": reasoning,
            "emotion": emotion,
            "action": action,
            "targets": targets
        }
        
        logs = []
        if os.path.exists(self.log_file):
            with open(self.log_file, 'r', encoding='utf-8') as f:
                try:
                    logs = json.load(f)
                except json.JSONDecodeError:
                    logs = []
        
        logs.append(log_entry)
        
        with open(self.log_file, 'w', encoding='utf-8') as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    def get_system_message(self, perceived_context):
        base_prompt = self.system_prompt + f"""
[Current Perception]
{perceived_context}

[Important Output Format]
당신의 응답은 반드시 다음 JSON 형식이어야 합니다. 다른 텍스트는 출력하지 마세요.
{{
    "content": "당신의 말투로 한 말. 상대방의 이름이나 호칭을 자연스럽게 포함시켜야 함.",
    "action_note": "당신의 행동이나 생각, 또는 상황에 대한 묘사. 예: '(한숨을 쉰다)', '(눈을 흘김)'",
    "emotion": "angry, happy, neutral, sad, etc.",
    "action": "yell, ask, ignore, etc."
}}

- content: 실제 말한 대사.
- action_note: 행동이나 생각 묘사.
- emotion: 당신의 현재 감정을 나타내는 영어 단어 (소문자)
- action: 당신의 행동을 나타내는 영어 단어 (소문자)
"""
        return {"role": "system", "content": base_prompt}

    def add_to_memory(self, message):
        self.memory.append(message)
        if len(self.memory) > 10:
            self.memory.pop(0)

# --- 채팅 엔진 ---
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.exceptions.RequestException, json.JSONDecodeError))
)
def chat_response(messages: list, model: str):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 16384,
        "temperature": 0.7,
        "stream": False
    }

    try:
        with requests.post(
            f"{BASE_URL}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json=payload
        ) as r:
            r.raise_for_status()
            data = r.json()
            
            if "choices" in data and len(data["choices"]) > 0:
                message_obj = data["choices"][0].get("message", {})
                content = message_obj.get("content", "")
                reasoning = message_obj.get("reasoning", "") or message_obj.get("reasoning_content", "")
                return content, reasoning
            else:
                logging.error(f"❌ 응답 구조 오류: {data}")
                raise ValueError("Invalid response structure")
                
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ 네트워크 오류: {e}")
        raise
    except json.JSONDecodeError as e:
        logging.error(f"❌ JSON 파싱 오류: {e}")
        raise
    except Exception as e:
        logging.error(f"❌ 알 수 없는 오류: {e}")
        raise

# --- JSON 파싱 함수 ---
def parse_json_response(content: str):
    try:
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        
        data = json.loads(content)
        
        clean_content = data.get("content", "")
        action_note = data.get("action_note", "")
        emotion = data.get("emotion", "neutral")
        action = data.get("action", "speak")
        
        return clean_content, action_note, emotion, action

    except json.JSONDecodeError:
        logging.warning(f"❌ JSON 파싱 실패. 원본 내용: {content}")
        return content, "", "neutral", "speak"

# --- 타겟 추출 함수 ---
def extract_targets_from_content(content: str, available_agents: dict):
    agent_names = list(available_agents.keys())
    targets = []
    for name in agent_names:
        pattern = r'\b' + re.escape(name) + r'\b'
        if re.search(pattern, content, re.IGNORECASE):
            targets.append(name)
            
    return targets if targets else ["self"]

# --- 물리적 거리 체크 ---
def are_proximate(pos1, pos2):
    proximate_pairs = [
        ("counter", "entrance"),
        ("entrance", "counter"),
        ("counter", "counter"),
        ("entrance", "entrance")
    ]
    return (pos1, pos2) in proximate_pairs

# --- 메인 시뮬레이션 로직 ---
def run_convenience_store_simulation():
    agents = {
        "boss": Agent(
            name="boss",
            system_prompt="너는 편의점 사장님 '김민태'(ID: kim)야. 47세 남자. 욕심이 많고 고지식함. 말투가 거칠고 불평이 많음.",
            initial_memory=[],
            position="counter"
        ),
        "lee": Agent(
            name="lee",
            system_prompt="너는 편의점 알바생 '이상민'(ID: lee)야. 29세 남자. 아이돌 지망생. 착실하고 씩씩하며 친절한 말투.",
            initial_memory=[],
            position="counter"
        ),
        "park": Agent(
            name="park",
            system_prompt="너는 편의점 알바생 '박슬기'(ID: park)야. 21세 여자. 통통튀고 발랄하며 에너지가 넘침.",
            initial_memory=[],
            position="counter"
        ),
        "customer": Agent(
            name="customer",
            system_prompt="너는 편의점 손님 '정용진'(ID: customer)야. 55세 남자. 불만이 많고 괴팍함. 말투가 거칠고 막무가내임.",
            initial_memory=[],
            position="entrance"
        ),
        "robot": Agent(
            name="robot",
            system_prompt="너는 편의점 AI 로봇 '로보-7'(ID: robot)야. 냉철하고 논리적이나 인간 관찰자. 기계적인 어조.",
            initial_memory=[],
            position="counter"
        )
    }

    perceived_contexts = {
        "boss": "상황: 아침 출근 시간. 사장님이 도착하여 문을 열었다. 이상민과 박슬기 알바생이 카운터 뒤에 서 있다. 정용진 손님이 입구에 들어섰다.\n사장님의 생각: '아, 또 월급 적다... 이 자식들 제때 왔냐?'\n사장님이 보이는 것: 이상민은 밝게 인사하고, 박슬기는 게으르게 서 있다.",
        "lee": "상황: 아침 출근 시간. 이상민이 카운터 뒤에 서 있다. 사장님이 들어오며 큰 소리로 인사한다. 박슬기는 게으르게 서 있다. 정용진 손님이 입구에 들어섰다.\n이상민의 생각: '아침부터 화이팅! 사장님도 기분이 좋으시다!'\n이상민이 보이는 것: 사장님이 큰 소리로 인사하시며, 박슬기가 게으르게 서 계신다.",
        "park": "상황: 박슬기가 카운터 뒤에 앉아 있다. 사장님이 들어오며 큰 소리로 불평한다. 이상민이 밝게 인사한다. 정용진 손님이 입구에 들어섰다.\n박슬기의 생각: '아... 시끄러워. 또 일해야 해.'\n박슬기가 보이는 것: 사장님이 큰 소리로 불평하시고, 이상민이 밝게 인사하신다.",
        "customer": "상황: 정용진 손님이 입구에 들어섰다. 사장님이 카운터 뒤에 서 있고, 알바생 두 명이 있다.\n손님의 생각: '아침부터 기분이 안 좋다. 커피 한 잔 빨리 달라.'\n손님이 보이는 것: 사장님과 알바생들이 서로 대화하며 바쁘게 움직이고 계신다.",
        "robot": "상황: 로보-7이 카운터 근처에 설치되어 있다. 사장님이 들어오며 큰 소리로 인사한다. 이상민과 박슬기가 반응한다. 정용진 손님이 입구에 들어섰다.\n로보-7의 생각: '인간들의 비효율적인 상호작용을 관찰 중.'\n로보-7이 보이는 것: 사장님이 큰 소리로 인사하시고, 알바생들이 반응하며, 손님이 들어오셨다."
    }

    agent_order = ["boss", "lee", "park", "customer", "robot"]
    edges = []

    print("🏪 편의점 시뮬레이션 시작 (Dual-Channel Communication)\n")
    print("="*50)

    for i, agent_key in enumerate(agent_order):
        turn = i + 1
        active_agent = agents[agent_key]
        
        print(f"\n📅 턴 {turn}: {active_agent.name}의 턴")
        print("-"*50)
        
        perceived_context = perceived_contexts[agent_key]
        
        # 메모리에서 clean_content와 action_note를 조합하여 대화 로그 구성
        memory_context = ""
        for msg in active_agent.memory:
            if msg['role'] == 'assistant':
                note = msg.get('action_note', '')
                if note:
                    combined_text = f"{msg['clean_content']} ({note})"
                else:
                    combined_text = msg['clean_content']
                memory_context += f"{combined_text}\n"
            elif msg['role'] == 'user':
                memory_context += f"{msg['content']}\n"

        call_messages = [
            active_agent.get_system_message(perceived_context),
            {"role": "user", "content": memory_context}
        ]

        print(f"👤 {active_agent.name}이(가) 응답을 생성 중입니다...")
        
        content, reasoning = chat_response(call_messages, MODEL)
        
        if content:
            clean_content, action_note, emotion, action = parse_json_response(content)
            targets = extract_targets_from_content(clean_content, agents)
            
            assistant_msg = {
                "role": "assistant",
                "content": content,
                "clean_content": clean_content,
                "action_note": action_note
            }
            active_agent.add_to_memory(assistant_msg)
            active_agent.add_to_log(assistant_msg, reasoning, emotion, action, targets)
            
            for target_name in targets:
                if target_name != "self":
                    target_agent = agents[target_name]
                    if are_proximate(active_agent.position, target_agent.position):
                        edges.append({
                            "source": active_agent.name,
                            "target": target_name,
                            "emotion": emotion,
                            "action": action,
                            "content": clean_content,
                            "timestamp": time.time()
                        })
            
            print(f"✅ {active_agent.name} -> {targets}: {clean_content}")
            if action_note:
                print(f"   [Action]: {action_note}")
            print(f"   [Emotion: {emotion}] [Action: {action}]")
            if reasoning:
                print(f"   [Thinking]: {reasoning[:50]}...")
        else:
            print("⚠️ 응답 생성 실패 또는 비어있음.")
        
        time.sleep(1)

    print("\n" + "="*50)
    print("🏁 시뮬레이션 종료")

if __name__ == "__main__":
    try:
        requests.get(f"{BASE_URL}/health", timeout=5).raise_for_status()
    except Exception as e:
        print(f"❌ 서버 연결 실패: {e}")
        sys.exit(1)
    
    run_convenience_store_simulation()

