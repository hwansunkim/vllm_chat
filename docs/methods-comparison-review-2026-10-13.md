# Rippletown Methods 초안 비교 리뷰

작성일: 2026년 10월 13일

## 비교 대상과 기준

- **A:** [Rippletown — Methods (draft).md](<Rippletown — Methods (draft).md>)
- **B:** [rippletown-methods-2026-10-13.md](rippletown-methods-2026-10-13.md)
- **리뷰 기준:** [Methods 리뷰 지침](methods-review-guidelines.md)
- **확인한 그림:** [Figure 1](../artifacts/simulator-structure/figure1-wave-en.png)

이 리뷰는 **docs 폴더 안의 두 문서**를 비교한 것이다. 앞서 다운로드 폴더의 동명 초안을 읽고 작성한 비교 평가는 이 문서의 근거로 사용하지 않는다. 원고와 코드는 수정하지 않았다.

**A를 기본 원고로 삼고, B에서 필요한 정확성 보완을 가져오는 방향을 권한다.** A는 이미 감독 제거, AI 직접 시간 추론, 일과 주입, 접촉 계산의 가정, 행동 고정 재분석을 반영했다. 코드에 존재하는 모든 옵션이나 구체적인 수치를 추가할 필요는 없다.

## 1. 전체 비교

| 항목 | A | B | 권장 |
| --- | --- | --- | --- |
| 핵심 설계 전달 | 관찰·접촉·시간과 연결해 구체적으로 설명 | 역할 구분은 정확하지만 추상적 표현이 많음 | A 중심 |
| 현재 연구 범위 | 핵심 방향을 반영했고 미사용 감독도 제거됨 | 동일하게 반영됨 | 큰 차이 없음 |
| 시간과 일과 | 사례와 목록이 있어 이해하기 쉬움 | 범위는 정확하지만 문장이 더 무거움 | A의 구성에 B의 한정 표현 일부 |
| 기억 | 처리 과정을 자연스럽게 설명 | 검색·표시·신뢰도를 더 정확히 구분 | A 중심, 일부 문장 교체 |
| 공간·지각·상태 | 직관적이지만 몇몇 표현이 지나치게 절대적 | 개념과 예외의 구분이 정확함 | B에서 짧게 보완 |
| 처리 순서 | Figure 1과 본문이 상호 보완 | 그림 없이 문장으로 설명 | A 우세 |
| 기록과 재분석 | 간결하고 구체적인 사례가 있음 | 분석 범위를 명확히 분리 | A의 간결함과 B의 구조 결합 |
| 문체 | 읽기 쉽고 설계 목적이 드러남 | 정확하지만 내부 구현 용어가 늘어남 | A 유지 |

## 2. A에서 살릴 좋은 점

### 연구 목적과 연결되는 첫 문단

> Where agents are, whom they are with and how long they stay together are recorded by the engine rather than narrated by the model.

이 문장은 엔진과 LLM의 역할 분리가 왜 접촉 기반 연구에서 중요한지 바로 전달한다. B의 생성된 행동과 세계 갱신을 분리한다는 설명보다 구체적이다.

다만 `how long they stay together`는 연속적인 실제 체류 시간을 정확히 추적한다는 인상을 줄 수 있다. 다음처럼 다듬으면 좋다.

> The engine records agents’ locations, co-presence and simulated durations, providing the basis for contact analysis.

### 일정 입력과 생성된 반응을 구분하는 사례

> whether it leaves on time, delays, or talks with others first

일정은 입력이지만 반응은 생성된다는 것을 구체적인 선택으로 설명한다. 이 사례는 유지할 가치가 있다. B의 같은 내용은 정확하지만 상대적으로 추상적이다.

### 시간 제한과 스케줄링의 목록 구성

A는 수치를 열거하지 않고도 독자가 각 규칙의 역할을 구분할 수 있도록 짧은 목록을 사용한다. B는 이를 문단으로 합치면서 한 번에 이해해야 할 내용이 많아졌다. A의 목록 형식은 유지해도 된다.

### 관계의 비대칭성을 설명하는 사례

아버지에게는 “wife”, 어머니에게는 “husband”라는 예시는 관계가 관찰자 관점에 따라 다르다는 것을 즉시 보여준다. B처럼 관계어를 나열하는 것보다 설명력이 있다.

## 3. B에서 가져올 핵심 보완

### 공간의 정의

A의 “organised in three levels”는 장소·구역·외부 공간이 세 단계로 중첩된 것처럼 읽힌다. 이 부분은 B의 정의가 더 정확하다.

> The world is a graph of named places grouped into zones, with designated exterior spaces.

그 뒤에는 A의 place는 co-presence, zone은 awareness라는 간결한 설명을 이어 붙이는 것이 좋다.

### 에이전트의 독립성

A의 다음 표현은 별도 모델 인스턴스와 완전한 정보 독립성을 암시한다.

> Each agent is an independent LLM instance.
>
> Agents share no hidden state.

B에서 필요한 부분만 가져오면 충분하다.

> Each agent has its own persona, memory and perceptual context, without direct access to other agents’ private memories.

별도 모델 프로세스 여부까지 본문에서 설명할 필요는 없다.

### 기억의 재등장과 신뢰도

A의 다음 표현은 재언급하면 신뢰도가 회복되는 것처럼 읽힐 수 있다.

> faded facts drop out of view and can return if they come up again

B는 표시에서 빠진 사실도 이후 검색 후보가 될 수 있다는 점을 정확히 설명한다. 다음처럼 짧게 바꿀 수 있다.

> Low-confidence facts may be omitted from the displayed memory while remaining available for later retrieval.

B의 신뢰도 갱신 설명 전체를 가져올 필요는 없다.

### 수면과 이동 상태의 구분

A는 상태 중에는 턴을 받지 않는다고 한 뒤, 직접 말을 걸면 깰 수 있다고 설명한다. 수면과 이동을 같은 종류의 잠금처럼 다뤄 약간 혼란스럽다.

B처럼 수면·개인 활동은 직접 호출로 중단될 수 있고, 이동은 도착할 때까지 일반 상호작용이 제한된다는 구분만 추가하면 충분하다.

## 4. A에서 남아 있는 수정 사항

다음은 문체 취향보다 의미의 정확성에 관련된 부분이다.

| 현재 표현 | 문제 | 최소 수정 방향 |
| --- | --- | --- |
| “At every turn … rebuilds the agent’s instructions” | 정적 계약과 매 턴 갱신되는 상황 정보를 구분하지 않음 | “The contract combines scenario-derived world rules with turn-specific instructions and context.” |
| Idle jump의 “still within those bounds” | 앞에서 설명한 모든 제한이 idle에도 동일하게 적용되는 것으로 읽힘 | 예정 이벤트와 상태 종료가 idle 진행을 제한한다고 범위를 좁힘 |
| “the clock jumps straight to the first moment someone becomes free” | 그보다 먼저 예정 이벤트가 발생할 수 있음 | “toward the next event or state ending”으로 완화 |
| “Relative words … are replaced” | 엔진이 확정적으로 치환한다는 인상을 줌 | compression prompt가 절대 날짜 표현을 요청한다고 설명 |
| 실패 메시지를 “restored and offered again” | 확인한 코드에서 rollback 이후 재전달을 보장하는 경로가 확인되지 않음 | 현재 Methods의 핵심과 거리가 있으므로 해당 문장 삭제 |
| “Nothing is heard across rooms” | 앞에서 허용한 작별 응답과 절대적 표현이 충돌함 | “Ordinary conversation requires co-presence.”로 원칙을 표현 |

실패 처리 문장은 부정형 경고로 바꿔 넣을 필요도 없다. 검토 시 확인한 [rollback 구현](../ABM/simulation/turn.py)은 working memory에서 메시지를 제거하는 처리였으며, 제거와 재전달은 구분해야 한다.

## 5. Figure 1 평가

Figure 1은 A의 큰 장점이다. LLM이 담당하는 단계와 엔진이 담당하는 단계를 한눈에 구분하고, 접촉 계산이 마지막에 이루어진다는 순서도 잘 보여준다. B에 없는 중요한 장점이므로 유지하는 것이 좋다.

### 상태 설명 수정

현재 그림 하단 문장:

> Agents in a sleep, private-activity or travelling state skip their turns; messages to them are held until the state ends.

이 문장은 본문이 설명하는 직접 말 걸기로 잠을 깨우는 경우까지 불가능한 것처럼 읽힌다. 다음 정도가 적절하다.

> Ongoing states limit turn scheduling and message delivery; direct addressing can interrupt sleep or private activity.

### 기억 압축 표현 수정

`Memory compressed when full`은 다음으로 바꾸는 것이 더 정확하면서 간단하다.

> Memory compressed as context fills

### 참여자 결정에 대한 캡션 수정

캡션의 “Scheduled events and state endings determine who joins the wave”는 두 요인만 참여자를 결정하는 것처럼 보일 수 있다.

> Messages from the preceding wave, scheduled events, state endings and scheduler re-invocation determine participation.

그림에 모든 예외를 넣을 필요는 없지만, 그림과 본문이 서로 다른 원칙을 제시하지 않게 해야 한다.

## 6. B에서 가져오지 않아도 되는 부분

### 내부 스케줄러 용어

`eligible for forced re-invocation`, `State-locked`, `eligible post-movement message delivery`는 독자가 내부 스케줄러를 이해해야 하는 듯한 느낌을 준다. A의 일상적인 언어가 더 좋다.

### 정확한 재개에 대한 긴 부정형 설명

A는 이미 “resumed from its recorded state”로 적절히 완화했다. 중단 없는 실행과 동일한 미래를 보장하지 않는다는 B의 긴 설명을 추가할 필요는 없다.

### 메모리 마지막 문단의 세부 구분

B는 압축 시각, 표시 기준, 검색 후보, 신뢰도 변화를 한꺼번에 설명해 밀도가 높다. A의 흐름을 유지하면서 오해가 생기는 문장만 수정하는 편이 좋다. 시간적 망각 자체를 주요 기여로 강조한다면 압축 시각에 기반한 recency 가정은 별도로 명확히 해야 한다.

### 관계 초기화 검증

B에 남아 있는 존재하지 않는 관계 참조와 self-reference 제거 설명은 현재 논문의 방법론적 초점에 비해 중요도가 낮다. 이를 삭제한 A의 선택이 더 적절하다.

## 7. 권장 조합

| 부분 | 채택 방향 |
| --- | --- |
| Framework overview | A 유지, 기록 시간 표현과 계약 생성 문장만 조정 |
| Time, waves and daily routines | A 유지, idle 제한과 종료 조건만 완화 |
| Space | B의 첫 문장과 A의 직관적인 설명 결합 |
| Agents | B의 독립된 문맥·기억 설명을 짧게 채택 |
| Memory | A 유지, 절대 날짜와 사실 재등장 표현만 수정 |
| Perception / States | A의 문체에 B의 핵심 구분 반영 |
| Agent roles / Social structure | A 중심, B의 관계어·인지·주관적 평가 구분을 한 문장 추가 |
| Wave processing | A와 Figure 1 유지, 그림의 상태 설명 보완 |
| Recording / Reanalysis | A의 내용을 별도 절로 분리하고 실패 재전달 문장 삭제 |

A는 이미 지침을 반영한 개정본이다. B로 전면 교체할 이유는 없다. A의 문체와 Figure 1을 유지하면서 B에서 공간 정의, 에이전트 독립성, 기억 검색, 상태 구분에 필요한 문장만 가져오는 것이 적절하다.
