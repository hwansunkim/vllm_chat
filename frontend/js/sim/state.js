// frontend/js/sim/state.js
// Shared simulation state and pure helpers (no module imports — avoid cycles).

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
  output_format_template: '',
  summary_interval: 0,
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
  eventSource:    null,
  scenarios:      [],
  agentEmotions:  {},   // { agent_name: latest_emotion } — updated per turn_complete
};

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
