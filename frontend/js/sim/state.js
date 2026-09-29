// frontend/js/sim/state.js
// Shared simulation state and pure helpers (no module imports — avoid cycles).
//
// 이 파일의 **순수 헬퍼 일부**는 ABM/export/labels.py 에 파이썬으로도 구현돼 있다
// (마크다운 내보내기가 브라우저와 CLI 양쪽에서 돌기 때문). 포팅된 것:
//   normalizeWeekday · normalizeProbability · normalizeBeta ·
//   normalizeSymptomStages · normalizeDurationSpec · buildInfectionModel ·
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

// ── 에이전트 상태(수면·개인 용무) 기본값 (백엔드 SimStartConfig 기본값과 동일하게 유지) ──
// id는 화면에 안 보이는 순수 내부 키다(time_categories와 같은 규칙) — 사용자는
// label·min_minutes·max_minutes·completion_style만 편집한다. 이동(zone 경계) 상태는
// 에이전트가 고르는 게 아니라 엔진이 자동으로 적용하므로 여기 포함되지 않는다(zoneTravel*).
// completion_style: 'completive'(기본) = 만료 시 "하던 일을 마쳤다: …"(자기완결형) /
// 'ongoing'("지속형" 체크박스) = "잠깐 정신이 들었다. (여전히 … 중)" — 실제 종료를 다른
// 신호(예정 이벤트 등)가 정하는 학업·업무류. 백엔드 StateCategory 의 기본값과 같은 값을
// **명시**해 둔다(생략해도 undefined 가 'ongoing' 이 아니라 동작은 같지만, 위 주석대로
// 두 목록은 모양까지 일치해야 한다). 수면은 전용 문구를 써서 이 값의 영향을 받지 않는다.
export const DEFAULT_STATE_CATEGORIES = [
  { id: 'sleep', label: '수면 — 상대가 알고도 말 걸지 않는 한 반응 없음', min_minutes: 300, max_minutes: 540, completion_style: 'completive' },
  { id: 'busy',  label: '자리를 비우고 하는 개인적인 일(씻기 등)',       min_minutes: 10,  max_minutes: 30,  completion_style: 'completive' },
];
export const DEFAULT_ZONE_TRAVEL_MIN_MINUTES = 10;
export const DEFAULT_ZONE_TRAVEL_MAX_MINUTES = 20;

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

// ── 감염병 모델 SEPIR (백엔드 InfectionModelConfig / SymptomStage와 1:1 대응) ───
// 상태는 S(감염 가능)→E(잠복기, 비전염)→P(무증상 전염기, 전염 가능)→I(감염기,
// 전염 가능)→R(회복) 순으로 전이한다. E/P/I 지속 시간은 DurationSpec(분포 종류 +
// 파라미터 + min~max 절단 범위, 일 단위)으로 설정한다 — 기본값은 SEPIR 도입 전의
// 연구 스펙 상수(E 절단 감마, P 0일 = 건너뜀, I 8일 고정)와 동일하다
// (ABM/simulation/infection.py 참고). 전염 확률은 λ=beta×t_d, P=1-exp(-λ)
// (Monte Carlo). 증상 진행은 "노출 후 경과 분" 기준이다.
export const DEFAULT_BETA = 0.04;

// DurationSpec 기본값 — ABM/export/labels.py 의 DEFAULT_*_DURATION 과 같은 값이어야 한다.
export const MAX_DURATION_DAYS = 36500;
export const DURATION_KINDS = ['uniform', 'gamma', 'gaussian'];
const _DURATION_BASE = { kind: 'uniform', shape: 2, scale: 1, mean: 5, stddev: 2, min_days: 1, max_days: 10 };
export const DEFAULT_EXPOSED_DURATION        = { ..._DURATION_BASE, kind: 'gamma', shape: 1.926, scale: 1.775, min_days: 1, max_days: 10 };
export const DEFAULT_PRESYMPTOMATIC_DURATION = { ..._DURATION_BASE, min_days: 0, max_days: 0 };
export const DEFAULT_INFECTIOUS_DURATION     = { ..._DURATION_BASE, min_days: 8, max_days: 8 };

const _round4 = n => Math.round(n * 10000) / 10000;

/**
 * DurationSpec 정규화 — labels.py normalize_duration_spec 과 동일 규칙.
 * kind 가 알 수 없으면 기본 kind, 양수 파라미터(shape/scale/stddev)가 0 이하·비숫자면
 * 기본값, min 은 0~36500, max < min 이면 min 을 max 로 낮춘다(증상 단계와 같은 규칙 —
 * 백엔드는 max < min 을 422 로 거부한다).
 */
export function normalizeDurationSpec(raw, def) {
  const src  = (raw && typeof raw === 'object') ? raw : {};
  const num  = v => { const n = typeof v === 'number' ? v : parseFloat(v); return Number.isFinite(n) ? n : null; };
  const pos  = k => { const n = num(src[k]); return n !== null && n > 0 ? _round4(n) : def[k]; };
  const any  = k => { const n = num(src[k]); return n !== null ? _round4(n) : def[k]; };
  const days = (k, fb) => { const n = num(src[k]); return n === null ? fb : Math.min(MAX_DURATION_DAYS, Math.max(0, _round4(n))); };
  let lo = days('min_days', def.min_days);
  const hi = days('max_days', def.max_days);
  if (hi < lo) lo = hi;
  return {
    kind:     DURATION_KINDS.includes(src.kind) ? src.kind : def.kind,
    shape:    pos('shape'),
    scale:    pos('scale'),
    mean:     any('mean'),
    stddev:   pos('stddev'),
    min_days: lo,
    max_days: hi,
  };
}

// 증상 문구 — 상태(E/P/I)별. 같은 status 항목은 등록 순서가 곧 그 상태 안의 진행 순서다
// (엔진이 "그 상태에 머문 비율"로 균등 분할해 고른다 — infection.py::_find_symptom_stage).
// "노출 후 경과분" 시간창은 없다. ABM/export/labels.py 의 DEFAULT_SYMPTOM_STAGES 와 같아야 한다.
// 백엔드 기본값은 빈 배열이지만 "감염 설정을 만든 적이 없는" 시나리오에는 바로 쓸 수 있는
// 기본 문구를 채운다. 사용자가 명시적으로 전부 지운 경우(빈 배열)는 그대로 존중한다.
export const SYMPTOM_STATUSES = ['E', 'P', 'I'];
export const SYMPTOM_STATUS_LABELS = { E: '잠복기(E)', P: '무증상 전염기(P)', I: '감염기(I)' };
export const DEFAULT_SYMPTOM_STAGES = [
  { status: 'E', symptom_text: '목이 조금 칼칼하고 살짝 피곤하다. 별일 아니겠지 싶은 정도다.' },
  { status: 'P', symptom_text: '특별히 아픈 데는 없지만 왠지 몸이 무겁게 느껴진다.' },
  { status: 'I', symptom_text: '열이 나고 기침이 멎지 않는다. 코가 막히고 목이 따갑다. 냄새와 맛이 잘 안 느껴진다.' },
  { status: 'I', symptom_text: '고열로 눈앞이 흐리다. 온몸이 쑤시고 기침이 심해 숨쉬기도 버겁다. 서 있기조차 힘들다.' },
];

// 모델 선택(UI 전용 — 엔진은 model_type을 읽지 않는다). 숨긴 구간은 지속 0으로 강제한다.
export const MODEL_TYPES = ['sir', 'seir', 'sepir'];
const _ZERO_DURATION = { kind: 'uniform', min_days: 0, max_days: 0 };
/** model_type별로 편집기를 보여줄 지속 시간 슬롯. */
export function visibleDurationKeys(modelType) {
  if (modelType === 'sir')  return ['infectious_duration'];
  if (modelType === 'seir') return ['exposed_duration', 'infectious_duration'];
  return ['exposed_duration', 'presymptomatic_duration', 'infectious_duration'];
}
/**
 * 이 상태가 **항상 건너뛰어지는지** — E/P는 지속이 0일 고정(max_days<=0)이거나 model_type이
 * 숨기는 구간이면 엔진이 그 상태를 거치지 않는다(infection.py::_set_infected 가 길이 0
 * 구간을 건너뛰고, 진행 판정도 0일 P를 건너뜀). 그 상태의 증상 문구는 쓰이지 않는다.
 * I는 0일이어도 한 번은 거친다(다음 판정에서 회복)라 건너뛰지 않는 것으로 본다.
 */
export function isStatusAlwaysSkipped(model, status) {
  const key = { E: 'exposed_duration', P: 'presymptomatic_duration' }[status];
  if (!key) return false;
  if (!visibleDurationKeys(model?.model_type).includes(key)) return true;
  return !(Number(model?.[key]?.max_days) > 0);
}

/**
 * model_type 에서 숨겨지는 구간의 지속 시간을 0~0일로 강제한다(제자리 수정 후 반환).
 * 예전에 SEPIR로 P>0을 설정해두고 SIR/SEIR로 바꾸면, 화면엔 안 보이는 P가 실제로는
 * 계속 동작하는 모순을 막는다. buildInfectionModel 이 항상 거치므로 저장·전송 경로 모두 보장된다.
 */
export function applyModelType(model) {
  const visible = visibleDurationKeys(model.model_type);
  if (!visible.includes('exposed_duration')) {
    model.exposed_duration = normalizeDurationSpec(_ZERO_DURATION, DEFAULT_EXPOSED_DURATION);
  }
  if (!visible.includes('presymptomatic_duration')) {
    model.presymptomatic_duration = normalizeDurationSpec(_ZERO_DURATION, DEFAULT_PRESYMPTOMATIC_DURATION);
  }
  return model;
}

/** 임의의 입력을 [0,1] 확률로 정규화. 비숫자는 fallback, 범위 밖은 클램프. */
export function normalizeProbability(v, fallback = 0) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return fallback;
  // 슬라이더 값(문자열)이 0.30000000000000004 같은 부동소수 잡음으로 저장되지 않도록 반올림.
  return Math.min(1, Math.max(0, Math.round(n * 100) / 100));
}

/**
 * 임의의 입력을 β(전염 확률 계수)로 정규화. 확률이 아니라 비율(rate)이라 1을
 * 넘을 수 있다 — 0 이상이기만 하면 된다(백엔드 `ge=0.0`과 동일). 기본값 0.04
 * 같은 소수점 자리를 보존하도록 4자리까지 반올림한다(normalizeProbability의
 * 2자리보다 정밀도가 더 필요함).
 */
export function normalizeBeta(v, fallback = 0) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(0, Math.round(n * 10000) / 10000);
}

/**
 * 증상 문구 목록 정규화 — labels.py normalize_symptom_stages 와 동일 규칙.
 * status 가 E/P/I 가 아닌 항목(구버전 min/max 형식 포함)은 버리고, E→P→I 순으로 안정
 * 정렬한다 — 같은 status 안의 상대 순서(= 진행 순서)는 그대로라 엔진 결과는 같다.
 */
export function normalizeSymptomStages(list) {
  if (!Array.isArray(list)) return [];
  const out = [];
  list.forEach(raw => {
    if (!raw || typeof raw !== 'object' || !SYMPTOM_STATUSES.includes(raw.status)) return;
    out.push({ status: raw.status, symptom_text: String(raw.symptom_text ?? '') });
  });
  // Array.prototype.sort 는 안정 정렬(ES2019+).
  return out.sort((a, b) => SYMPTOM_STATUSES.indexOf(a.status) - SYMPTOM_STATUSES.indexOf(b.status));
}

/**
 * 임의의 입력을 백엔드 InfectionModelConfig 모양으로 정규화.
 * 저장/전송/불러오기의 모든 경로가 이 함수 하나를 통과한다 — 구버전 시나리오처럼
 * 필드 자체가 없으면(raw == null) 기본값 전체를 채운 "꺼진 모델"을 돌려준다.
 */
export function buildInfectionModel(raw) {
  const src = (raw && typeof raw === 'object') ? raw : null;
  return applyModelType({
    enabled:                  !!src?.enabled,
    disease_name:             String(src?.disease_name ?? '').trim(),
    beta:                     normalizeBeta(src?.beta, DEFAULT_BETA),
    // 감염 설정 자체가 없던 시나리오만 기본 단계로 채운다(위 주석 참고).
    symptom_stages:           src && Array.isArray(src.symptom_stages)
                                ? normalizeSymptomStages(src.symptom_stages)
                                : DEFAULT_SYMPTOM_STAGES.map(s => ({ ...s })),
    immune_after_recovery:    src?.immune_after_recovery ?? true,
    exposed_duration:         normalizeDurationSpec(src?.exposed_duration,        DEFAULT_EXPOSED_DURATION),
    presymptomatic_duration:  normalizeDurationSpec(src?.presymptomatic_duration, DEFAULT_PRESYMPTOMATIC_DURATION),
    infectious_duration:      normalizeDurationSpec(src?.infectious_duration,     DEFAULT_INFECTIOUS_DURATION),
    model_type:               MODEL_TYPES.includes(src?.model_type) ? src.model_type : 'sepir',
  });
}

/**
 * infection_update 이벤트 → 화면 뱃지. 표시할 게 없으면 null.
 * status='S'는 "한 번도 안 걸림"과 "회복했지만 재감염 가능(SEIRS)" 두 가지 의미라
 * cause로 구분한다 — 전자는 뱃지를 달지 않는다.
 */
export function infectionBadge(status, cause) {
  if (status === 'E') return { icon: '⏳', label: '잠복기',      cls: 'exposed'   };
  if (status === 'P') return { icon: '😶', label: '무증상 전염기', cls: 'presymptomatic' };
  if (status === 'I') return { icon: '🦠', label: '감염',        cls: 'infected'  };
  if (status === 'R') return { icon: '💚', label: '회복·면역',    cls: 'recovered' };
  if (status === 'S' && cause === 'recovery') return { icon: '💚', label: '회복', cls: 'recovered' };
  return null;
}

/**
 * agent_status_change 이벤트 → 카드 뱃지. action='clear'거나 알 수 없는 state면
 * 표시할 게 없다(null) — 그 자리에서 뱃지를 지운다는 신호로 호출부가 해석한다.
 * traveling만 고정 아이콘/라벨을 쓰고, 나머지(자기-선언형 카테고리)는 서버가
 * 함께 보낸 label을 그대로 쓴다 — 카테고리 id는 사용자가 설정 화면에서 마음대로
 * 바꾸는 값이라 프론트가 라벨 텍스트를 새로 짓지 않는다.
 */
export function agentStatusBadge(d) {
  if (!d || d.action !== 'enter') return null;
  if (d.state === 'traveling') return { icon: '🚶', label: '이동 중', cls: 'traveling' };
  return { icon: '💤', label: d.label || d.state || '상태', cls: 'busy' };
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
  // 공간 기반 인지. 'targeted'(기본) = 발화가 지목된 상대에게만 전달(기존 동작),
  // 'spatial' = 같은 장소 제3자의 엿듣기 + 같은 zone 다른 장소로의 대사만 전달 +
  // 혼잣말의 행동 관찰. 위치 그래프(방/zone) 위에서만 의미가 있는 옵션이다.
  perception_mode: 'targeted',
  lang_fix_enabled: true,
  lang_fix_retries: 2,
  // 출력 **계약** 오버라이드. '' = 엔진이 실행 시점에 현재 설정으로 생성(기본).
  // 값이 있으면 그 문자열이 출력 형식 계약만 대체한다 — 지도·시간·감염 계약은
  // 오버라이드와 무관하게 계속 엔진이 자동 최신화한다.
  // (구 `output_format_template` 은 폐기 — 백엔드가 읽지 않고 저장 시 비운다.)
  output_format_override: '',
  sim_start_time:    '09:00',  // 시뮬레이션 시작 시각 (HH:MM)
  sim_start_weekday: 'mon',    // 시뮬레이션 시작 요일 ('mon'~'sun'). 자정 롤오버 시 서버가 자동 증가
  time_per_wave:     30,       // wave당 경과 시간(분). 0 = 시간 개념 비활성 (time_mode='fixed'일 때만 사용)
  time_mode: 'fixed',          // 'fixed' = wave당 고정 시간, 'variable' = wave 내용을 LLM이 분류해 가변 경과
  time_categories: DEFAULT_TIME_CATEGORIES.map(c => ({ ...c })),      // time_mode='variable'일 때 분류 카테고리
  // 'category'(기본) = LLM이 위 카테고리 중 하나를 골라 그 범위에서 무작위 경과분,
  // 'ai' = LLM이 경과분을 직접 추론(카테고리 전체 min~max로 clamp). time_mode='variable'일 때만 의미 있음.
  time_estimation_mode: 'category',
  idle_minutes_schedule: [...DEFAULT_IDLE_MINUTES_SCHEDULE],          // 강제 침묵 재투입 시 경과 시간(분) 스케줄
  // 가변 시간 점프 상한 — LLM 분류기가 고른 카테고리의 랜덤 경과분을 엔진이 벽시계·
  // 동석 상황 기준으로 캡한다(schemas.py SimStartConfig와 동일 기본값). 0 = 캡 비활성.
  max_scene_jump_minutes:   45,   // 실내 한 곳에 2명+ 동석 발화 중일 때
  max_daytime_jump_minutes: 180,  // 밤(22~06시)이 아니고 집에 남은 사람이 있을 때
  // 압축 전(raw) 메모리의 시간 앵커 — 압축된 기억에는 이미 날짜 헤더가 붙지만
  // 아직 압축되지 않은 최근 대화에는 시간 정보가 없다. 켜면 자정을 넘거나 문턱값
  // 이상 시간이 점프할 때만 "[시간] 3일차 수요일 오후 7시 20분" 같은 라벨 한 줄을
  // 그 에이전트 기억에 남긴다(schemas.py SimStartConfig와 동일 기본값).
  memory_time_anchor_enabled:           false,
  memory_time_anchor_threshold_minutes: 45,
  // 소외 재투입 간격(wave) — 대화가 오가는 중에도 마지막 턴 이후 이 wave 수 이상
  // 지난(상태에 묶이지 않은) 에이전트를 빈 incoming 으로 재투입한다(구 max_silence_waves
  // "고립 휴면 기준"을 대체). 대화가 시들해지는 것만으로는 시뮬레이션이 끝나지
  // 않는다(종료: max_waves / target_duration / no_agents / no_progress).
  starvation_waves:   3,
  // 에이전트 상태(수면·이동 등) — 재투입(전원 침묵 처리)이 상태를 모른 채 무조건
  // 다시 초대해서 이미 잠든 에이전트가 잠꼬대를 반복하거나 zone 경계 이동이
  // 순간이동처럼 보이는 문제를 막는다. []면(생략과 다름) 자기-선언형 상태
  // 기능 자체가 꺼진다.
  state_categories: DEFAULT_STATE_CATEGORIES.map(c => ({ ...c })),
  zone_travel_min_minutes: DEFAULT_ZONE_TRAVEL_MIN_MINUTES,
  zone_travel_max_minutes: DEFAULT_ZONE_TRAVEL_MAX_MINUTES,
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
    digest_waves:          6,    // 디렉터가 개입 판단 시 되짚는 최근 wave 수 (엔진 clamp [2,20])
  },
  // 결정론적 감염병 모델(SEIR/SEIRS). enabled=false면 서버에서 상태 갱신도 프롬프트 주입도
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

// ── Relationship helpers (관계 지도) ───────────────────────────────────────────
// AgentConfig.relationships = { 상대 agent의 name(key): "내가 그를 부르는 관계" }.
// 각자 **자기 시점**이라 대칭일 필요가 없다(김봉남→채민경 "아내", 채민경→김봉남 "남편").
// key 는 언제나 시스템 ID(name)이고 display_name 이 아니다 — 엔진이 name 으로 조회한다.
// 빈 객체 = 기능 미사용이며, 그때 프롬프트는 관계 도입 전과 글자 단위로 같다.

/** 어떤 값이 와도 평범한 { key: string } 객체로 만든다 (구버전 시나리오/손으로 쓴 JSON 대비). */
export function normalizeRelationships(v) {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
  const out = {};
  for (const [k, label] of Object.entries(v)) {
    const key = String(k).trim();
    if (!key) continue;                       // 빈 key 는 엔진에서 조회 불가 — 버린다
    out[key] = label == null ? '' : String(label);
  }
  return out;
}

/**
 * 관계 key 가 dangling 인가 = 엔진(Simulation._sanitize_relationships)이 조용히 버릴 key 인가.
 * 판정 규칙은 엔진과 동일: "현재 에이전트 목록에 없다" 또는 "자기 자신".
 */
export function isDanglingRelationshipKey(key, selfName) {
  if (!key) return true;
  if (key === selfName) return true;
  return !sim.agents.some(a => a.name === key);
}

/** dangling 을 걷어낸 관계 지도 — 실행 시점에 실제로 계약에 실리는 것과 같다. */
export function liveRelationships(agent) {
  if (!agent) return {};
  const out = {};
  for (const [k, v] of Object.entries(normalizeRelationships(agent.relationships))) {
    if (!isDanglingRelationshipKey(k, agent.name)) out[k] = v;
  }
  return out;
}

/**
 * 엔진의 `_key_to_alias` 와 **같은 2단계**로 만든 표시명 맵 { name: display_name }.
 * headless.py 등은 `{a.display_name: a.name}` 정방향 맵을 먼저 만들고 뒤집는다 —
 * display_name 이 겹치면 정방향에서 충돌해 한 명만 살아남는 것까지 재현해야
 * "미리보기 = 실제 주입본" 약속이 유지된다.
 */
export function buildKeyToAlias() {
  const fwd = {};
  for (const a of sim.agents) {
    const d = (a.display_name || '').trim();
    if (d) fwd[d] = a.name;
  }
  const map = {};
  for (const [d, n] of Object.entries(fwd)) map[n] = d;
  return map;
}
