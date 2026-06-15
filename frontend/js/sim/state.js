// frontend/js/sim/state.js
// Shared simulation state and pure helpers (no module imports — avoid cycles).

export const sim = {
  status:              'idle',
  selectedAgent:       null,
  currentScenarioId:   null,
  currentScenarioName: '',
  agents:       [],
  background:   '',
  start_agent:  '',
  max_waves:    10,
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
  server_id:        null,   // null = 기본 서버, string = 특정 서버 ID
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
