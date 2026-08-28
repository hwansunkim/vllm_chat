// frontend/js/agent-transfer.js
// 채팅 에이전트(`/api/agents`) ↔ 시뮬레이션 에이전트(`AgentConfig`, 시나리오 JSON 안)
// 사이의 순수 변환 로직.
//
// 두 스키마는 백엔드에서 합집합으로 확장되어 있어 이름이 같은 필드는 1:1로 옮기면 된다.
// 유일하게 개념이 다른 게 모델 지정이다:
//
//   채팅        model: str        — 모델 이름 문자열 하나
//   시뮬레이션   server_id: str   — 등록된 LLM 서버 인스턴스의 id
//
// 그래서 이 모듈은 서버 목록(GET /api/servers, 각 row 에 {id, name, model, enabled})을
// 받아 둘 사이를 다리 놓는다. 시뮬레이션 AgentConfig 에는 원본 `model` 문자열도 함께
// 보존하므로, 채팅 → 시뮬레이션 → 채팅 왕복에서 서버 매칭이 어긋나도 원래 모델명이
// 살아남는다.
//
// 가져오기는 전부 스냅샷 복사다 — 가져온 시점의 값만 옮겨지고 이후 한쪽을 고쳐도
// 다른 쪽에는 반영되지 않는다.

import { normalizeAgentTemperature, normalizeTemperature } from './sim/state.js';

// 채팅 에이전트 폼 기본값 (backend AgentCreate 기본값과 동일하게 유지).
const CHAT_DEFAULT_MAX_TOKENS = 1024;

/** 이미 쓰이는 이름이면 `-2`, `-3` … 을 붙여 충돌을 피한다. */
export function uniqueName(base, takenNames) {
  const taken = new Set(takenNames || []);
  const root  = (base || 'agent').trim() || 'agent';
  if (!taken.has(root)) return root;
  let n = 2;
  while (taken.has(`${root}-${n}`)) n++;
  return `${root}-${n}`;
}

/**
 * 채팅 에이전트 응답 → 시뮬레이션 AgentConfig.
 *
 * 이름이 같은 필드는 그대로 옮긴다. `server_id`는 자동 매칭하지 않고 항상
 * null(= 시뮬레이션 기본 서버)로 둔다 — 예전엔 `model` 문자열로 서버를 추측해서
 * 채웠는데, 같은 모델을 서빙하는 서버가 여럿이면 매칭이 흔들리고(왕복할 때마다
 * 다른 서버로 표류) 사용자가 의도하지 않은 서버가 조용히 골라지는 문제가 있었다.
 * 필요하면 가져온 뒤 카드에서 직접 서버를 고르는 편이 명확하다.
 * 원본 `model` 문자열은 AgentConfig.model에 그대로 보존해 채팅으로 되돌릴 때 쓴다
 * (사용자가 나중에 이 카드에 서버를 직접 지정하면 그 서버의 모델명이 우선한다 —
 * `simAgentToChatBody` 참고).
 *
 * @param {object} chat  GET /api/agents 응답 항목
 * @param {object} opts  { takenNames }
 * @returns {{agent: object}}
 */
export function chatAgentToSimAgent(chat, { takenNames = [] } = {}) {
  const agent = {
    // ── 시뮬레이션이 실제로 쓰는 필드 ──
    name:               uniqueName(chat.name, takenNames),
    system_prompt:      chat.system_prompt || '',
    icon:               chat.icon || '🤖',
    gender:             chat.gender || 'auto',
    initial_active:     chat.initial_active !== false,
    display_name:       chat.display_name || '',
    groups:             Array.isArray(chat.groups) ? [...chat.groups] : [],
    location:           chat.location || '',
    visual_description: chat.visual_description || '',
    server_id:          null,   // 항상 기본 서버로 시작 — 필요하면 사용자가 직접 지정
    temperature:        normalizeAgentTemperature(chat.temperature),
    // ── ABM 엔진이 해석하지 않는, 왕복 보존 전용 필드 ──
    role:               chat.role        || null,
    goal:               chat.goal        || null,
    backstory:          chat.backstory   || null,
    description:        chat.description || null,
    model:              (chat.model || '').trim() || null,
    max_tokens:         Number.isFinite(+chat.max_tokens) ? +chat.max_tokens : null,
  };
  return { agent };
}

/**
 * 시뮬레이션 AgentConfig → POST /api/agents 본문.
 *
 * `server_id` 는 채팅 스키마에 없으므로 모델명으로 되돌린다:
 *   1) server_id가 실제 서버로 풀리면(= 사용자가 카드에서 직접 고른 현재 의도) 그 서버의
 *      모델명을 우선한다. server_id가 바뀌어도 AgentConfig.model은 갱신되지 않으므로
 *      (agents.js의 server_id 변경 핸들러 참고), 예전 model을 먼저 보면 사용자가 방금
 *      바꾼 서버가 아니라 가져오기 당시의 낡은 모델명이 조용히 되살아난다.
 *   2) server_id가 없거나 못 찾으면(삭제/비활성 등) 보존된 AgentConfig.model로 폴백.
 *   3) 둘 다 없으면 비워둔다(= 채팅 기본 모델 사용).
 *
 * @param {object} simAgent           시나리오 config.agents[i]
 * @param {object} opts               { servers, fallbackTemperature }
 * @returns {{body: object, server: object|null, modelSource: 'server'|'agent-model'|'none'}}
 */
export function simAgentToChatBody(simAgent, { servers = [], fallbackTemperature } = {}) {
  const server = simAgent.server_id
    ? (Array.isArray(servers) ? servers : []).find(s => s.id === simAgent.server_id) || null
    : null;

  const own = (simAgent.model || '').trim();
  let model = '';
  let modelSource = 'none';
  if (server && (server.model || '').trim()) {
    model = server.model.trim();
    modelSource = 'server';
  } else if (own) {
    model = own;
    modelSource = 'agent-model';
  }

  // 에이전트별 온도가 비어 있으면(= 시뮬레이션 기본값 상속) 시나리오의 기본값을 굳혀 넣는다.
  const temperature = normalizeAgentTemperature(simAgent.temperature)
    ?? normalizeTemperature(fallbackTemperature);

  const body = {
    name:               simAgent.name || 'agent',
    description:        simAgent.description || '',
    system_prompt:      simAgent.system_prompt || '',
    icon:               simAgent.icon || '🤖',
    model:              model || null,
    temperature,
    max_tokens:         Number.isFinite(+simAgent.max_tokens) && +simAgent.max_tokens > 0
                          ? +simAgent.max_tokens : CHAT_DEFAULT_MAX_TOKENS,
    // 채팅 전용 필드 — 시뮬레이션에는 보존만 되어 있던 값을 되살린다.
    role:               simAgent.role      || '',
    goal:               simAgent.goal      || '',
    backstory:          simAgent.backstory || '',
    // 시뮬레이션 전용 필드 — 채팅 UI 에는 입력란이 없고 그대로 보존만 된다.
    gender:             simAgent.gender || 'auto',
    groups:             Array.isArray(simAgent.groups) ? [...simAgent.groups] : [],
    location:           simAgent.location || '',
    visual_description: simAgent.visual_description || '',
    display_name:       simAgent.display_name || '',
    initial_active:     simAgent.initial_active !== false,
  };

  return { body, server, modelSource };
}

/** 모델 출처를 사용자에게 보여줄 한국어 문구로. */
export function describeModelSource(modelSource, server, model) {
  switch (modelSource) {
    case 'agent-model':
      return `모델 "${model}" (시뮬레이션에 보존돼 있던 원본 모델명)`;
    case 'server':
      return `모델 "${model}" (시뮬레이션 서버 "${server?.name ?? '?'}" 에서 가져옴)`;
    default:
      return '모델 미지정 — 채팅 기본 모델을 사용합니다.';
  }
}
