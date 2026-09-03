// frontend/js/sim/state.js
// Shared simulation state and pure helpers (no module imports — avoid cycles).
//
// 이 파일의 **순수 헬퍼 일부**는 ABM/export/labels.py 에 파이썬으로도 구현돼 있다
// (마크다운 내보내기가 브라우저와 CLI 양쪽에서 돌기 때문). 포팅된 것:
//   normalizeWeekday · normalizeDurationMinutes · normalizeProbability ·
//   normalizeSymptomStages · buildInfectionModel · formatDayHour ·
//   infectionBadge · meetingNarration · detectGender · getAgentIcon ·
//   agentLabel · simTimeLabel
// 이 중 하나라도 문구/규칙을 바꾸면 파이썬 쪽도 같이 고칠 것 —
// tests/fixtures/*.md 골든 테스트가 어긋난 쪽을 잡아낸다.

// ── 가변 시간 모드 기본값 (백엔드 SimStartConfig 기본값과 동일하게 유지) ──────────
export const DEFAULT_TIME_CATEGORIES = [
  { id: 'meal_or_brief',      label: '식사·짧은 용무',      min_minutes: 5,   max_minutes: 10  },
  { id: 'normal_scene',       label: '일반적인 대화/활동',   min_minutes: 15,  max_minutes: 30  },
  { id: 'alone_or_offscreen', label: '혼자 있음/외출',      min_minutes: 60,  max_minutes: 120 },
  { id: 'night_sleep',        label: '취침/장시간 경과',     min_minutes: 240, max_minutes: 420 },
];
export const DEFAULT_IDLE_MINUTES_SCHEDULE = [60, 120, 180];

// ── 요일 (백엔드 SimStartConfig.sim_start_weekday Literal과 정확히 동일해야 함) ──
// 이 7개 소문자 코드 외의 값을 보내면 API가 422를 반환한다.
export const WEEKDAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
export const WEEKDAY_LABELS = {
  mon: '월요일', tue: '화요일', wed: '수요일', thu: '목요일',
  fri: '금요일', sat: '토요일', sun: '일요일',
};
export const DEFAULT_START_WEEKDAY = 'mon';

/** 임의의 입력을 유효한 요일 코드로 정규화 (알 수 없으면 'mon'). */
export function normalizeWeekday(v) {
  const k = String(v ?? '').toLowerCase();
  return WEEKDAY_KEYS.includes(k) ? k : DEFAULT_START_WEEKDAY;
}

// ── 샘플링 온도 (백엔드 SimStartConfig.temperature / AgentConfig.temperature와 동일) ──
// 범위를 벗어난 값을 보내면 API가 422를 반환하므로, UI에서 상태로 읽어들이는 모든 경로가
// 아래 정규화 함수를 통과하도록 한다.
export const DEFAULT_TEMPERATURE = 0.7;
export const TEMPERATURE_MIN = 0.0;
export const TEMPERATURE_MAX = 2.0;

/** 시뮬레이션 레벨 온도로 정규화. 비숫자는 기본값(0.7), 범위 밖은 [0,2]로 클램프. */
export function normalizeTemperature(v) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return DEFAULT_TEMPERATURE;
  return Math.min(TEMPERATURE_MAX, Math.max(TEMPERATURE_MIN, n));
}

/**
 * 에이전트별 온도 오버라이드로 정규화.
 * 빈 값/비숫자 = null(= 시뮬레이션 기본값 사용) — server_id의 "" → null 규칙과 같은 의미.
 */
export function normalizeAgentTemperature(v) {
  if (v === null || v === undefined || String(v).trim() === '') return null;
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return null;
  return Math.min(TEMPERATURE_MAX, Math.max(TEMPERATURE_MIN, n));
}

// ── 목표 기간 (백엔드 SimStartConfig / SimContinueConfig의 target_duration_minutes) ──
// 백엔드는 "분 단위 정수(ge=1)" 또는 null만 받는다. 0/음수는 422이므로 "사용 안 함"은
// 반드시 null로 보내야 한다 — server_id의 ""→null, 에이전트 temperature의 빈 값→null과 같은 규칙.
// UI는 사람이 쓰기 편한 (숫자 + 단위)로 입력받고 여기서 분으로 환산한다.
export const DURATION_UNITS = [
  { id: 'day',   label: '일',   minutes: 1440   },
  { id: 'week',  label: '주',   minutes: 10080  },  // 7일
  { id: 'month', label: '개월', minutes: 43200  },  // 30일
  { id: 'year',  label: '년',   minutes: 525600 },  // 365일
];
export const DEFAULT_DURATION_UNIT = 'day';
// 100년 — 이보다 큰 값은 사실상 입력 실수다. 상한이 없으면 큰 숫자가 JSON
// 직렬화 시 지수 표기(예: 5.256e+26)로 바뀌어 백엔드에서 422가 난다.
export const MAX_TARGET_DURATION_MINUTES = 52560000;

export function durationUnitMinutes(unitId) {
  const u = DURATION_UNITS.find(x => x.id === unitId);
  return u ? u.minutes : 1440;
}

/**
 * 임의의 입력을 target_duration_minutes로 정규화.
 * 빈 값/비숫자/0 이하 = null(= 목표 기간 미사용). 그 외에는 1 이상의 정수(분).
 */
export function normalizeTargetDuration(v) {
  if (v === null || v === undefined || String(v).trim() === '') return null;
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return null;
  const i = Math.round(n);
  if (i < 1) return null;
  return Math.min(i, MAX_TARGET_DURATION_MINUTES);
}

/**
 * 분 → { value, unit, exact } 역산 (시나리오를 불러올 때 입력 폼을 복원하는 용도).
 * 큰 단위부터 검사해 "딱 떨어지는" 가장 큰 단위를 고른다(20160분 → 2주, 43200분 → 1개월).
 * 어떤 단위로도 나누어떨어지지 않으면 일 단위 근사치를 돌려주고 exact=false로 표시한다 —
 * 호출부는 이때 사용자가 입력을 건드리지 않는 한 원본 분 값을 그대로 다시 저장해야 한다
 * (근사치로 덮어쓰면 저장할 때마다 값이 조금씩 달라진다).
 */
export function minutesToDurationParts(minutes) {
  const m = normalizeTargetDuration(minutes);
  if (m === null) return { value: '', unit: DEFAULT_DURATION_UNIT, exact: true };
  for (let i = DURATION_UNITS.length - 1; i >= 0; i--) {
    const u = DURATION_UNITS[i];
    if (m % u.minutes === 0) return { value: m / u.minutes, unit: u.id, exact: true };
  }
  const approxDays = Math.max(0.01, Math.round((m / 1440) * 100) / 100);
  return { value: approxDays, unit: 'day', exact: false };
}

/** (숫자, 단위) → 분. 빈 값/0 이하는 null(= 미사용). */
export function durationPartsToMinutes(value, unitId) {
  const raw = typeof value === 'number' ? value : String(value ?? '').trim();
  if (raw === '') return null;
  const n = typeof raw === 'number' ? raw : parseFloat(raw);
  if (!Number.isFinite(n) || n <= 0) return null;
  return normalizeTargetDuration(n * durationUnitMinutes(unitId));
}

// ── 일 + 시간 복합 입력 (감염병 모델의 경과 시간 필드 전용) ────────────────────
// target_duration_minutes가 (숫자 + 단위 셀렉트)인 것과 달리, 증상 단계/회복 시간은
// "2일 12시간"처럼 두 칸을 동시에 채우는 편이 자연스럽다. 저장은 언제나 분(int).
// durationPartsToMinutes()와 달리 0을 null로 바꾸지 않는다 — 여기서는 0이 유효한 값이다
// (min_minutes=0 = 감염 즉시, recovery_max_minutes=0 = 자연 회복 없음).
const MINUTES_PER_DAY  = 1440;
const MINUTES_PER_HOUR = 60;

/** 임의의 입력을 [0, MAX_TARGET_DURATION_MINUTES] 범위의 정수 분으로. 비숫자는 fallback. */
export function normalizeDurationMinutes(v, fallback = 0) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(MAX_TARGET_DURATION_MINUTES, Math.max(0, Math.round(n)));
}

/** (일, 시간) → 분. 빈 칸/비숫자는 0으로 본다(둘 다 비면 0분). */
export function dayHourToMinutes(days, hours) {
  const d = normalizeDurationMinutes(String(days ?? '').trim() === '' ? 0 : days);
  const h = normalizeDurationMinutes(String(hours ?? '').trim() === '' ? 0 : hours);
  return normalizeDurationMinutes(d * MINUTES_PER_DAY + h * MINUTES_PER_HOUR);
}

/**
 * 분 → { days, hours } 역산 (입력 폼 복원용).
 * 시간 미만(분 단위 나머지)은 두 칸으로 표현할 수 없어 버려진다 — 화면에 보이는 값과
 * 저장값이 어긋나지 않도록, 호출부는 사용자가 그 칸을 건드릴 때만 다시 분으로 환산한다.
 */
export function minutesToDayHour(minutes) {
  const m = normalizeDurationMinutes(minutes);
  return {
    days:  Math.floor(m / MINUTES_PER_DAY),
    hours: Math.floor((m % MINUTES_PER_DAY) / MINUTES_PER_HOUR),
  };
}

/** 분 → "2일 12시간" 사람이 읽는 문자열 (0분은 "0시간"). */
export function formatDayHour(minutes) {
  const { days, hours } = minutesToDayHour(minutes);
  if (!days && !hours) return '0시간';
  return [days ? `${days}일` : '', hours ? `${hours}시간` : ''].filter(Boolean).join(' ');
}

/**
 * 시간 개념 자체가 꺼져 있는지 판정 — 이때만 백엔드가 목표 기간을 무시한다.
 * 주의: time_mode='variable'이면 wave당 시간이 0이어도 LLM 분류로 시간이 흐르므로 활성이다.
 * (time_per_wave === 0 하나만 보고 판단하면 안 된다.)
 */
export function isTimeConceptDisabled(timeMode, timePerWave) {
  const raw = typeof timePerWave === 'number' ? timePerWave : parseInt(timePerWave);
  // 백엔드(ABM/simulation/core.py)는 `max(0, int(time_per_wave))`로 음수를 0으로
  // 클램프한 뒤 활성 여부를 판단한다. 여기서 음수를 그대로 두면 `!(-5)`가 false라
  // "활성"으로 오판정돼(백엔드는 무시하는데 프론트는 목표 기간이 동작한다고 안내).
  const tpw = Math.max(0, Number.isFinite(raw) ? raw : 0);
  return timeMode !== 'variable' && !tpw;
}

// ── 감염병 모델 (백엔드 InfectionModelConfig / SymptomStage와 1:1 대응) ────────
// 전염 확률은 서버가 `ge=0.0, le=1.0`으로 검증한다 — 범위 밖 값은 422이므로
// 상태로 읽어들이는 모든 경로가 normalizeProbability()를 통과하도록 한다.
// 전염만 wave·접촉 기준이고, 증상 진행과 회복은 "감염 후 경과 분" 기준이다.
export const DEFAULT_TRANSMISSION_PROBABILITY = 0.3;

// 회복까지 걸리는 시간 — 감염 시점에 [min, max]분에서 균등 샘플되어 확정된다.
// max === 0은 "자연 회복 없음(만성)"이라는 별도 의미라 min과 비교하지 않는다(백엔드도 허용).
export const DEFAULT_RECOVERY_MIN_MINUTES = 7200;   // 5일
export const DEFAULT_RECOVERY_MAX_MINUTES = 14400;  // 10일

// 백엔드 기본값은 빈 배열이지만, 단계가 하나도 없는 채로 감염 모델을 켜면 주입할 서사가
// 없어 에이전트가 자기 몸 상태를 영영 인지하지 못한다. 그래서 "감염 설정을 만든 적이 없는"
// 시나리오에는 바로 쓸 수 있는 3단계를 채워준다(time_categories가 항상 4슬롯을 채워
// 보내는 것과 같은 규칙). 사용자가 명시적으로 전부 지운 경우(빈 배열)는 그대로 존중한다.
// 구간은 "감염 후 경과 분"이고 양끝을 포함한다. 첫 단계는 반드시 0분에서 시작해야
// 감염 직후에도 증상이 주입된다(min_minutes > 0이면 그 전까지는 증상 없음).
export const DEFAULT_SYMPTOM_STAGES = [
  { id: 'incubation', label: '잠복기', min_minutes: 0,    max_minutes: 2880,  // 0 ~ 2일
    symptom_text: '목이 조금 칼칼하다. 피곤해서 그런 거겠지, 별일 아닐 것이다.' },
  { id: 'onset',      label: '발현기', min_minutes: 2880, max_minutes: 7200,  // 2일 ~ 5일
    symptom_text: '몸이 으슬으슬하고 기침이 멎지 않는다. 이마가 뜨겁다.' },
  { id: 'acute',      label: '급성기', min_minutes: 7200, max_minutes: 20160, // 5일 ~ 14일
    symptom_text: '고열로 눈앞이 흐리다. 온몸이 쑤시고 서 있기조차 버겁다.' },
];

/** 임의의 입력을 [0,1] 확률로 정규화. 비숫자는 fallback, 범위 밖은 클램프. */
export function normalizeProbability(v, fallback = 0) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return fallback;
  // 슬라이더 값(문자열)이 0.30000000000000004 같은 부동소수 잡음으로 저장되지 않도록 반올림.
  return Math.min(1, Math.max(0, Math.round(n * 100) / 100));
}

/**
 * 증상 단계 목록 정규화.
 * 백엔드는 max_minutes < min_minutes를 422로 거부하므로(TimeCategory와 같은 검증 패턴)
 * 여기서 미리 바로잡는다. id는 엔진이 쓰지 않지만 편집 UI의 키라 비지 않고 유일해야 한다.
 */
export function normalizeSymptomStages(list) {
  if (!Array.isArray(list)) return [];
  const seen = new Set();
  const out  = [];
  list.forEach((raw, i) => {
    if (!raw || typeof raw !== 'object') return;
    let id = String(raw.id ?? '').trim() || `stage${i + 1}`;
    if (seen.has(id)) id = `${id}_${i + 1}`;
    seen.add(id);
    let min = normalizeDurationMinutes(raw.min_minutes, 0);
    let max = normalizeDurationMinutes(raw.max_minutes, min);
    // min을 max에 맞춰 낮춘다(그 반대가 아니라) — 사용자가 방금 고친 쪽은 보통 max이고,
    // max를 min까지 끌어올리면 화면에 남은 값과 실제 전송값이 어긋나며, 끌어올린 값이
    // 다른 단계 구간과 겹치거나 그 사이에 공백을 만들 수 있다.
    if (max < min) min = max;
    out.push({
      id,
      label:        String(raw.label ?? '').trim() || id,
      min_minutes:  min,
      max_minutes:  max,
      symptom_text: String(raw.symptom_text ?? ''),
    });
  });
  return out;
}

/**
 * 임의의 입력을 백엔드 InfectionModelConfig 모양으로 정규화.
 * 저장/전송/불러오기의 모든 경로가 이 함수 하나를 통과한다 — 구버전 시나리오처럼
 * 필드 자체가 없으면(raw == null) 기본값 전체를 채운 "꺼진 모델"을 돌려준다.
 */
export function buildInfectionModel(raw) {
  const src = (raw && typeof raw === 'object') ? raw : null;
  let recMin = normalizeDurationMinutes(src?.recovery_min_minutes, DEFAULT_RECOVERY_MIN_MINUTES);
  const recMax = normalizeDurationMinutes(src?.recovery_max_minutes, DEFAULT_RECOVERY_MAX_MINUTES);
  // max === 0은 "자연 회복 없음(만성)"이라는 별도 의미 — 백엔드가 이 경우만 min과의
  // 대소 검증을 건너뛴다. 그 외에는 증상 단계와 같은 규칙으로 min을 max까지 낮춘다.
  if (recMax > 0 && recMax < recMin) recMin = recMax;
  return {
    enabled:                  !!src?.enabled,
    disease_name:             String(src?.disease_name ?? '').trim(),
    transmission_probability: normalizeProbability(src?.transmission_probability, DEFAULT_TRANSMISSION_PROBABILITY),
    // 감염 설정 자체가 없던 시나리오만 기본 단계로 채운다(위 주석 참고).
    symptom_stages:           src && Array.isArray(src.symptom_stages)
                                ? normalizeSymptomStages(src.symptom_stages)
                                : DEFAULT_SYMPTOM_STAGES.map(s => ({ ...s })),
    recovery_min_minutes:     recMin,
    recovery_max_minutes:     recMax,
    immune_after_recovery:    src?.immune_after_recovery ?? true,
  };
}

/**
 * infection_update 이벤트 → 화면 뱃지. 표시할 게 없으면 null.
 * status='S'는 "한 번도 안 걸림"과 "회복했지만 재감염 가능(SIS)" 두 가지 의미라
 * cause로 구분한다 — 전자는 뱃지를 달지 않는다.
 */
export function infectionBadge(status, cause) {
  if (status === 'I') return { icon: '🦠', label: '감염',        cls: 'infected'  };
  if (status === 'R') return { icon: '💚', label: '회복·면역',    cls: 'recovered' };
  if (status === 'S' && cause === 'recovery') return { icon: '💚', label: '회복', cls: 'recovered' };
  return null;
}

/**
 * meeting_update 이벤트 → 관전자 시점 한 줄 서술. 표시할 게 없으면 null.
 * 피드 카드(run/feed.js)와 마크다운 내보내기(export/markdown.js)가 같은 문구를 쓰도록
 * 여기 한 곳에서만 만든다 (infectionBadge와 같은 위치·같은 이유).
 *
 * target_name은 chaser의 인지 상태에 따라 실명일 수도 `낯선 이(ID: "stranger_2")`일 수도
 * 있어 그대로 쓴다. 조사는 기존 씬 문구와 마찬가지로 받침 판정을 하지 않는다.
 * 모르는 status는 null → 구버전/미래 값이 와도 카드가 생기지 않고 조용히 무시된다.
 */
export function meetingNarration(d) {
  if (!d || !d.chaser) return null;
  const chaser = d.chaser_name || agentLabel(d.chaser);
  const target = d.target_name || (d.target ? agentLabel(d.target) : '');
  if (!target) return null;

  if (d.status === 'start') {
    const where = d.target_location ? ` (${d.target_location})` : '';
    return { icon: '🏃', cls: 'start', text: `${chaser}가 ${target}를 만나러 이동 중${where}` };
  }
  if (d.status === 'arrived') {
    return { icon: '🤝', cls: 'arrived', text: `${chaser}가 ${target}와 만났다` };
  }
  if (d.status === 'cancelled') {
    return d.reason === 'gone'
      ? { icon: '💨', cls: 'cancelled', text: `${chaser}가 ${target}를 찾았지만 자리를 뜬 뒤였다` }
      : { icon: '↩️', cls: 'cancelled', text: `${chaser}가 ${target}를 만나려던 것을 그만뒀다` };
  }
  return null;
}

export const sim = {
  status:              'idle',
  selectedAgent:       null,
  currentScenarioId:   null,
  currentScenarioName: '',
  agents:       [],
  background:   '',
  start_agent:  '',
  max_waves:    10,            // 이번 실행의 wave 상한 (안전장치). 목표 기간과 함께 쓰면 먼저 도달하는 쪽에서 종료
  target_duration_minutes: null, // 목표 기간(분). null = 미사용. 이번 실행 기준 예산(max_waves와 동일한 성격)
  step_delay:   1.0,
  token_limit:    8192,
  llm_max_tokens: 16384,
  extra_fields: [
    { name: 'emotion',     default: 'neutral' },
    { name: 'action',      default: 'speak'   },
    { name: 'action_note', default: ''        },
  ],
  events:       [],
  location_graph: [],
  lang_fix_enabled: true,
  lang_fix_retries: 2,
  // 출력 **계약** 오버라이드. '' = 엔진이 실행 시점에 현재 설정으로 생성(기본).
  // 값이 있으면 그 문자열이 출력 형식 계약만 대체한다 — 지도·시간·감염 계약은
  // 오버라이드와 무관하게 계속 엔진이 자동 최신화한다.
  // (구 `output_format_template` 은 폐기 — 백엔드가 읽지 않고 저장 시 비운다.)
  output_format_override: '',
  summary_interval: 5,   // 디렉터의 장거리 서사 신호. 0 = 비활성 (schemas.py와 동일 기본값)
  sim_start_time:    '09:00',  // 시뮬레이션 시작 시각 (HH:MM)
  sim_start_weekday: 'mon',    // 시뮬레이션 시작 요일 ('mon'~'sun'). 자정 롤오버 시 서버가 자동 증가
  time_per_wave:     30,       // wave당 경과 시간(분). 0 = 시간 개념 비활성 (time_mode='fixed'일 때만 사용)
  time_mode: 'fixed',          // 'fixed' = wave당 고정 시간, 'variable' = wave 내용을 LLM이 분류해 가변 경과
  time_categories: DEFAULT_TIME_CATEGORIES.map(c => ({ ...c })),      // time_mode='variable'일 때 분류 카테고리
  idle_minutes_schedule: [...DEFAULT_IDLE_MINUTES_SCHEDULE],          // 강제 침묵 재투입 시 경과 시간(분) 스케줄
  max_silence_waves:  3,        // 연속 침묵 허용 wave 수 (early_stop_enabled + time_per_wave > 0일 때 활성)
  early_stop_enabled: true,    // false = 조기 종료 비활성 (max_waves까지 항상 실행)
  server_id:        null,   // null = 기본 서버, string = 특정 서버 ID
  temperature:      0.7,    // 시뮬레이션 전체 기본 샘플링 온도 (0.0~2.0). 에이전트별로 오버라이드 가능
  system_agent: {
    enabled:               false,
    icon:                  '🎬',
    display_name:          '내레이터',
    system_prompt:         '',    // 비어있으면 백엔드 DEFAULT_SYSTEM_AGENT_PROMPT 사용
    intervention_interval: 1,
    silence_threshold:     3,
    director_note:         '',   // 시뮬레이션 서사 목표
  },
  // 결정론적 감염병 모델(SIR/SIS). enabled=false면 서버에서 상태 갱신도 프롬프트 주입도
  // 전혀 일어나지 않는다(infect_agent 이벤트도 조용히 무시된다).
  infection_model: buildInfectionModel(null),
  eventSource:    null,
  scenarios:      [],
  agentEmotions:  {},   // { agent_name: latest_emotion } — updated per turn_complete
  // 이번 실행에서 발생한 오류 누적 로그 (오래된 것 → 최신 순).
  // { kind: 'turn'|'connection', turn, speaker, error, timestamp } — 상한 MAX_ERROR_LOG.
  // 시뮬레이션을 새로 시작할 때 clearErrorLog()로 초기화된다 (run/errors.js).
  errorLog:       [],
  // { agent_name: { status, cause, wave, disease_name } } — infection_update SSE로 갱신.
  // 감염 뱃지·그래프/지도 노드 강조가 모두 이 맵 하나를 본다.
  agentInfection: {},
};

// 오류 로그 보관 상한. 시나리오가 막힐 때는 같은 오류가 매 턴 반복되므로
// 무한히 쌓지 않고 최신 N건만 남긴다.
export const MAX_ERROR_LOG = 50;

// Accordion expand state (keyed by agent.name)
export const _expandedAgents = new Set();

// ── Emotion helpers ───────────────────────────────────────────────────────────
export const EMOTION_COLORS = {
  angry: '#ef4444', happy: '#22c55e', neutral: '#94a3b8',
  sad: '#3b82f6', fear: '#f97316',
};
const EMOTION_CLASS = ['angry', 'happy', 'neutral', 'sad', 'fear'];

export function emotionColor(e) { return EMOTION_COLORS[e] || '#a78bfa'; }
export function emotionClass(e) { return EMOTION_CLASS.includes(e) ? `emotion-${e}` : 'emotion-neutral'; }

// ── Auto-icon system ──────────────────────────────────────────────────────────
const _GENDER_BASE = { male: '👨', female: '👩', unknown: '🧑' };

const _EMOTION_FACE = {
  happy:        '😊',
  sad:          '😢',
  angry:        '😠',
  fear:         '😨',
  surprised:    '😲',
  excited:      '😄',
  calm:         '😌',
  worried:      '😟',
  anxious:      '😰',
  embarrassed:  '😳',
  disappointed: '😞',
  frustrated:   '😤',
  confused:     '🤔',
  proud:        '😎',
};

const _MALE_KW   = ['남성', '남자', '남편', '아들', '아버지', '아빠', '형', '오빠', '삼촌', '할아버지', '소년', '남학생', '남동생', '사내', '남성형', '그는'];
const _FEMALE_KW = ['여성', '여자', '아내', '딸', '어머니', '엄마', '언니', '누나', '이모', '할머니', '소녀', '여학생', '여동생', '아가씨', '여인', '그녀는', '그녀의'];

export function detectGender(text) {
  if (!text) return 'unknown';
  const m = _MALE_KW.filter(k => text.includes(k)).length;
  const f = _FEMALE_KW.filter(k => text.includes(k)).length;
  if (m > f) return 'male';
  if (f > m) return 'female';
  return 'unknown';
}

export function getAgentIcon(agent, emotion) {
  if (agent.icon && agent.icon !== '🤖') return agent.icon;
  const g = (agent.gender === 'auto' || !agent.gender)
    ? detectGender((agent.system_prompt || '') + ' ' + (agent.display_name || ''))
    : agent.gender;
  const base = _GENDER_BASE[g] || '🧑';
  const face = _EMOTION_FACE[emotion || 'neutral'];
  return face ? base + face : base;
}

// ── HTML escape (kept here for zero-dependency module access) ─────────────────
export function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── 시뮬레이션 시각 계산 ──────────────────────────────────────────────────────
// 주의: fixed 모드 전용 클라이언트 추정치. variable 모드에서는 wave당 시간이 균일하지
// 않아 이 공식으로 계산할 수 없으므로 null을 반환한다(호출부가 "…" 등으로 처리).
// 서버가 turn_complete 이벤트/로그 항목에 실어 보내는 `time_str`(실제 계산값)이 있으면
// 항상 그것을 우선 사용하고, 이 함수는 time_str이 없는 구버전 로그에 대한 폴백으로만 쓸 것.
// 반환 포맷은 서버 `_format_time_str()`과 동일: `{요일} {오전|오후} {시}시 {분:02d}분`.
// 요일은 시작 요일(sim.sim_start_weekday) + 자정 경과 일수로 계산한다. 구버전 로그에는
// 당시 시작 요일 정보가 없으므로 현재 설정값 기준의 근사치이며, 목적은 신규/구버전 로그의
// 화면 표기를 일관되게 맞추는 것이다.
export function simTimeLabel(waveNum) {
  if (sim.time_mode === 'variable') return null;
  const tpw = sim.time_per_wave ?? 30;
  if (!tpw) return null;
  const [h, m] = (sim.sim_start_time || '09:00').split(':').map(Number);
  const startMin = (h || 0) * 60 + (m || 0);
  const DAY = 24 * 60;
  const totalMin  = startMin + waveNum * tpw;
  const dayOffset = Math.floor(totalMin / DAY);
  const total     = ((totalMin % DAY) + DAY) % DAY;
  const startIdx  = WEEKDAY_KEYS.indexOf(normalizeWeekday(sim.sim_start_weekday));
  const wd        = WEEKDAY_LABELS[WEEKDAY_KEYS[(((startIdx + dayOffset) % 7) + 7) % 7]];
  const hour = Math.floor(total / 60);
  const min  = total % 60;
  const pad  = String(min).padStart(2, '0');
  if (hour < 12) return `${wd} 오전 ${hour}시 ${pad}분`;
  const dh = hour === 12 ? 12 : hour - 12;
  return `${wd} 오후 ${dh}시 ${pad}분`;
}

// ── Misc small helpers ────────────────────────────────────────────────────────
export function fmtK(n) {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// ── Agent display helpers ──────────────────────────────────────────────────────
/** display_name이 있으면 display_name, 없으면 name(ID) 반환 */
export function agentLabel(key) {
  const a = sim.agents.find(ag => ag.name === key);
  return (a && a.display_name) ? a.display_name : (a ? a.name : key);
}

/** 아이콘 + 표시 이름 */
export function agentLabelWithIcon(key) {
  const a = sim.agents.find(ag => ag.name === key);
  if (!a) return key;
  return `${getAgentIcon(a, sim.agentEmotions[a.name])} ${a.display_name || a.name}`;
}

// ── Group helpers ─────────────────────────────────────────────────────────────
/** 현재 에이전트 목록에서 사용 중인 그룹 ID를 수집 (자동완성용) */
export function getAllGroups() {
  const groups = new Set();
  for (const agent of sim.agents) {
    for (const g of (agent.groups || [])) groups.add(g);
  }
  return [...groups].sort();
}
