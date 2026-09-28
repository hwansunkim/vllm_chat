// frontend/js/sim/config.js
// 시뮬레이션 설정 직렬화 — 저장(/scenarios POST·PUT), 파일 내보내기, 실행(/start)이
// 모두 이 함수 하나를 쓴다. 예전에는 저장 경로(scenarios.js)와 실행 경로(run/control.js)가
// 필드 목록을 각자 나열해서, 실행 요청에서만 state_categories·zone_travel_*가 빠져
// UI 설정이 무시되고 백엔드 기본값으로 실행되는 버그가 있었다.
//
// 계약: 반환 객체의 키 = backend/api/simulation/schemas.py 의 SimStartConfig 필드 −
//   · scenario_id           — 실행 전용. 저장본 config 에는 넣지 않고 startSimulation()이
//                             `{ scenario_id, ...buildSimConfig() }` 로 앞에 붙인다.
//   · output_format_template — 구 필드. 백엔드가 불러올 때 output_format_override 로
//                             옮긴다(backend/api/simulation/scenarios.py). 새로 보내지 않는다.
// tests/test_frontend_payload.py 가 이 계약을 정적으로 검사한다 — 스키마에 필드를
// 추가하면 여기에도 추가해야 테스트가 통과한다.
//
// 의존성은 state.js 뿐이다(scenarios.js·control.js 어느 쪽도 import 하지 않음 → 순환 없음).
//
// 기본값 정책:
//   · 0 / 빈 배열이 유효한 값인 필드는 `??` 또는 Array.isArray 로 보존한다
//     (state_categories: [] = 상태 기능 off, zone_travel_*: 0 = 즉시 이동 등).
//   · 빈 문자열이 유효하지 않은 필드(sim_start_time, server_id)는 `||` 로 기본값 처리한다
//     — '' 시각은 엔진의 split(':')에서 깨지고, '' server_id 는 "기본 서버"(null)의 뜻이다.

import { sim, DEFAULT_TIME_CATEGORIES, DEFAULT_IDLE_MINUTES_SCHEDULE, normalizeWeekday,
         normalizeTemperature, normalizeTargetDuration, buildInfectionModel,
         normalizeRelationships, DEFAULT_STATE_CATEGORIES,
         DEFAULT_ZONE_TRAVEL_MIN_MINUTES, DEFAULT_ZONE_TRAVEL_MAX_MINUTES } from './state.js';

/**
 * 현재 `sim` 상태를 SimStartConfig 모양의 평범한 객체로 직렬화한다.
 * 호출 전에 readConfigFromUI()로 화면 값을 sim에 반영해 두는 것은 호출자 책임이다.
 * @param {object} [s=sim] 직렬화할 상태 객체 (테스트용 주입점)
 */
export function buildSimConfig(s = sim) {
  return {
    // 관계 지도만 정규화해서 내보낸다 — 카드 편집기가 만든 값은 이미 평범한 객체지만,
    // 파일로 가져온 시나리오/구버전 데이터에는 필드가 아예 없을 수 있다. 나머지 필드는
    // 스프레드로 그대로 보존한다(role/goal 같은 "왕복 보존 전용" 필드 포함).
    agents:                 (s.agents || []).map(a => ({ ...a, relationships: normalizeRelationships(a.relationships) })),
    background:             s.background,
    start_agent:            s.start_agent,
    max_waves:              s.max_waves,
    // 목표 기간(분). "사용 안 함"은 반드시 null — 0/음수는 백엔드가 422로 거부한다.
    target_duration_minutes: normalizeTargetDuration(s.target_duration_minutes),
    step_delay:             s.step_delay,
    token_limit:            s.token_limit,
    llm_max_tokens:         s.llm_max_tokens,
    extra_fields:           s.extra_fields,
    events:                 s.events,
    location_graph:         s.location_graph || [],
    // 'spatial' = 같은 방 엿듣기 + 같은 zone 다른 방으로의 대사 전달 + 혼잣말 행동 관찰.
    // 'targeted'(기본)이면 엔진이 기존 라우팅 경로를 그대로 탄다.
    perception_mode:        s.perception_mode === 'spatial' ? 'spatial' : 'targeted',
    lang_fix_enabled:       s.lang_fix_enabled ?? true,
    lang_fix_retries:       s.lang_fix_retries ?? 2,
    output_format_override: s.output_format_override || '',
    // '' 는 유효한 시각이 아니다(엔진이 'HH:MM'을 split) — `||` 로 기본값.
    sim_start_time:         s.sim_start_time || '09:00',
    sim_start_weekday:      normalizeWeekday(s.sim_start_weekday),
    time_per_wave:          s.time_per_wave ?? 30,   // 0 = 시간 개념 off (유효값)
    time_mode:              s.time_mode ?? 'fixed',
    time_categories:        (s.time_categories?.length ? s.time_categories : DEFAULT_TIME_CATEGORIES),
    // time_mode='variable'일 때만 의미 있음. 'ai' = LLM이 카테고리 대신 경과분을 직접 추론.
    time_estimation_mode:   s.time_estimation_mode === 'ai' ? 'ai' : 'category',
    idle_minutes_schedule:  (s.idle_minutes_schedule?.length ? s.idle_minutes_schedule : DEFAULT_IDLE_MINUTES_SCHEDULE),
    max_scene_jump_minutes:   s.max_scene_jump_minutes   ?? 45,
    max_daytime_jump_minutes: s.max_daytime_jump_minutes ?? 180,
    // raw 메모리 시간 앵커. 0 은 유효값("자정 경계에서만 기록")이라 `??`.
    memory_time_anchor_enabled:           !!s.memory_time_anchor_enabled,
    memory_time_anchor_threshold_minutes: s.memory_time_anchor_threshold_minutes ?? 45,
    starvation_waves:       s.starvation_waves ?? 3,
    // time_categories와 달리 **빈 배열이 유효한 값**이다(자기-선언형 상태 기능 off) —
    // 배열이면 그대로 보존. 값 자체가 없을(undefined/null) 때만 기본 상태로 폴백한다
    // (조용히 []가 되어 기능이 꺼지는 것을 막는다).
    state_categories:       Array.isArray(s.state_categories)
      ? s.state_categories : DEFAULT_STATE_CATEGORIES.map(c => ({ ...c })),
    // 0 은 유효값 — `??`.
    zone_travel_min_minutes: s.zone_travel_min_minutes ?? DEFAULT_ZONE_TRAVEL_MIN_MINUTES,
    zone_travel_max_minutes: s.zone_travel_max_minutes ?? DEFAULT_ZONE_TRAVEL_MAX_MINUTES,
    // '' = 셀렉트의 "기본 서버" — null 로 보낸다.
    server_id:              s.server_id || null,
    temperature:            normalizeTemperature(s.temperature),
    system_agent:           s.system_agent,
    // 전염 확률은 0~1, 모든 분 값은 0~52560000이고 max >= min — 벗어나면 서버가 422로 거부한다.
    infection_model:        buildInfectionModel(s.infection_model),
  };
}
