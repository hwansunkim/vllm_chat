import { state } from './state.js';
import { api, probeModels } from './api.js';
import { esc } from './utils.js';

let _statusServers = [];

// ── Model status + Thinking button ──────────────────────────────────────────

export async function loadModelStatus() {
  try {
    const data = await api('GET', '/model/status');
    _statusServers = data.servers || [];

    const name = data.model ? data.model.split('/').pop() : '없음';
    document.getElementById('model-badge').textContent =
      name + (data.max_model_len ? ` · ${(data.max_model_len / 1000).toFixed(0)}K ctx` : '');

    const serverName = data.current_server?.name ?? '서버 없음';
    document.getElementById('current-server-name').textContent = serverName;

    updateThinkingBtn(data.current_server?.thinking ?? false);
    renderServerDropdown(_statusServers);
  } catch {
    document.getElementById('model-badge').textContent = '연결 오류';
    document.getElementById('current-server-name').textContent = '연결 오류';
  }
}

export function updateThinkingBtn(supportsThinking) {
  state.currentServerThinking = supportsThinking;
  const btn = document.getElementById('thinking-btn');
  if (!state.currentConvId) return;

  if (!supportsThinking) {
    if (state.thinkingEnabled) {
      state.thinkingEnabled = false;
      btn.classList.remove('active');
    }
    btn.disabled = true;
    btn.title = '이 서버는 Thinking 모드를 지원하지 않습니다';
  } else {
    btn.disabled = false;
    btn.title = state.thinkingEnabled ? '사고 모드 끄기' : '사고 모드 켜기';
  }
}

// ── Server dropdown ──────────────────────────────────────────────────────────

function renderServerDropdown(servers) {
  const listEl = document.getElementById('server-dropdown-list');
  if (!servers.length) {
    listEl.innerHTML = '<div style="padding:12px 14px;font-size:13px;color:#94a3b8;">등록된 서버 없음</div>';
    return;
  }
  listEl.innerHTML = servers.map(s => {
    const isActive  = s.is_default;
    const modelShort = s.model.split('/').pop();
    const ctxInfo   = s.model_len ? ` · ${(s.model_len / 1000).toFixed(0)}K ctx` : '';
    const thinkTag  = s.thinking ? ' <span style="font-size:10px;color:#d97706">🧠</span>' : '';
    return `
      <div class="server-dropdown-item ${isActive ? 'active' : ''}" data-id="${s.id}">
        <span class="ss-check">${isActive ? '✓' : ''}</span>
        <div class="ss-info">
          <div class="ss-name">${esc(s.name)}${thinkTag}</div>
          <div class="ss-model">${esc(modelShort)}${esc(ctxInfo)}</div>
        </div>
      </div>`;
  }).join('');
}

async function switchDefaultServer(serverId) {
  if (_statusServers.find(s => s.id === serverId)?.is_default) return;
  document.querySelectorAll('.server-dropdown-item').forEach(el => el.classList.add('switching'));
  try {
    await api('PUT', `/servers/${serverId}`, { is_default: true });
    closeServerDropdown();
    await loadModelStatus();
  } catch (e) {
    alert('서버 전환 실패: ' + e.message);
    document.querySelectorAll('.server-dropdown-item').forEach(el => el.classList.remove('switching'));
  }
}

function toggleServerDropdown() {
  const dd  = document.getElementById('server-dropdown');
  const btn = document.getElementById('server-selector-btn');
  if (!dd.classList.contains('hidden')) {
    closeServerDropdown();
  } else {
    dd.classList.remove('hidden');
    btn.classList.add('open');
  }
}

function closeServerDropdown() {
  document.getElementById('server-dropdown').classList.add('hidden');
  document.getElementById('server-selector-btn').classList.remove('open');
}

// ── Server modal ─────────────────────────────────────────────────────────────

let _serverList   = [];
let _editServerId = null;

// provider_type 별 폼 취급 규칙. 백엔드 계약(_workspace/backend_done.md 2-5)과 1:1 대응한다.
const PROVIDERS = {
  vllm: {
    label: 'vLLM',
    badge: '🖥️ vLLM',
    hint: '자체 호스팅 vLLM 서버. Base URL 이 반드시 필요합니다.',
    urlRequired: true,
    urlLabel: 'Base URL *',
    urlPlaceholder: '예: http://172.17.3.135:8000',
    defaultBaseUrl: '',
    apiKeyLabel: 'API Key (없으면 빈칸)',
    apiKeyPlaceholder: '예: local',
    thinkingEnabled: true,
    thinkingLabel: 'Thinking 모드 (chat_template_kwargs 전송)',
    // vLLM 은 보통 모델 1개만 서빙하고 주소를 아는 사용자가 모델명도 안다 → 직접 입력 유지
    canProbeModels: false,
    filterNonChatModels: false,
  },
  openai: {
    label: 'OpenAI',
    badge: '🟢 OpenAI',
    hint: 'Base URL 을 비우면 https://api.openai.com/v1 을 사용합니다. 컨텍스트 길이 자동 감지는 되지 않습니다.',
    urlRequired: false,
    urlLabel: 'Base URL (선택)',
    urlPlaceholder: '비워두면 https://api.openai.com/v1 사용',
    defaultBaseUrl: 'https://api.openai.com/v1',
    apiKeyLabel: 'API Key * (없으면 연결 실패)',
    apiKeyPlaceholder: '예: sk-...',
    thinkingEnabled: false,
    thinkingLabel: 'Thinking 모드 — 이 프로바이더에서는 지원하지 않습니다',
    canProbeModels: true,
    // /v1/models 에 embedding·tts·whisper·dall-e 등이 섞여 온다
    filterNonChatModels: true,
  },
  anthropic: {
    label: 'Anthropic',
    badge: '🟣 Anthropic',
    hint: 'Base URL 을 비우면 https://api.anthropic.com/v1 을 사용합니다. 컨텍스트 길이 미입력 시 200K 로 가정합니다.',
    urlRequired: false,
    urlLabel: 'Base URL (선택)',
    urlPlaceholder: '비워두면 https://api.anthropic.com/v1 사용',
    defaultBaseUrl: 'https://api.anthropic.com/v1',
    apiKeyLabel: 'API Key * (없으면 연결 실패)',
    apiKeyPlaceholder: '예: sk-ant-...',
    thinkingEnabled: true,   // openai 와 달리 extended thinking 을 지원한다
    thinkingLabel: 'Extended thinking 사용 (budget 10k 토큰)',
    canProbeModels: true,
    // Anthropic 은 챗 모델만 반환한다
    filterNonChatModels: false,
  },
};

const DEFAULT_PROVIDER = 'vllm';

function providerMeta(type) {
  return PROVIDERS[type] ?? PROVIDERS[DEFAULT_PROVIDER];
}

// ── 모델 목록 조회 (POST /servers/probe-models) ───────────────────────────────

// 명백히 챗 용도가 아닌 모델만 걸러낸다. 애매하면(realtime/audio/search 등 새 변종)
// 포함시킨다 — 새 모델을 실수로 숨기는 쪽이 노이즈보다 나쁘다.
const NON_CHAT_MODEL_PATTERNS = [
  'embedding', 'embed-',
  'whisper', 'tts', 'transcribe', 'speech',
  'dall-e', 'moderation',
  'davinci-002', 'babbage-002',
  'image-1', 'sora', '-instruct', 'computer-use',
];

function isChatCandidateModel(id) {
  const lower = String(id).toLowerCase();
  return !NON_CHAT_MODEL_PATTERNS.some(p => lower.includes(p));
}

function setProbeStatus(text, isError = false) {
  const el = document.getElementById('sv-probe-status');
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
  el.classList.toggle('hidden', !text);
}

/** datalist 와 상태 문구를 초기화한다 (provider 전환 / 폼 재오픈 시). */
function clearProbedModels() {
  document.getElementById('sv-model-options').innerHTML = '';
  setProbeStatus('');
}

// provider 전환/재조회/폼 닫기 이후에 도착하는 stale 응답을 버리기 위한 세대 토큰.
// (예: openai 로 조회 시작 → 응답 전에 anthropic 으로 전환 → 늦게 온 openai
//  결과가 anthropic 폼의 datalist 를 덮어쓰는 경합을 막는다)
let _probeSeq = 0;

function fillModelDatalist(ids) {
  const listEl = document.getElementById('sv-model-options');
  listEl.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const id of ids) {
    const opt = document.createElement('option');
    opt.value = id;          // value 는 이스케이프 없이 안전하게 들어간다 (DOM API)
    frag.appendChild(opt);
  }
  listEl.appendChild(frag);
}

async function onProbeModelsClick() {
  const btn = document.getElementById('sv-probe-btn');
  if (btn.disabled) return;

  const seq = ++_probeSeq;
  const providerType = document.getElementById('sv-provider').value;
  const meta = providerMeta(providerType);
  // 편집 모드에서 #sv-api-key 는 항상 빈 값으로 시작한다(마스킹 키 prefill 금지).
  // 비어 있으면 서버가 저장된 키(server_id 로 조회) → 환경변수 순으로 폴백한다.
  const apiKey   = document.getElementById('sv-api-key').value.trim();
  const baseUrl  = document.getElementById('sv-url').value.trim();
  const serverId = _editServerId;

  const original = btn.textContent;
  btn.textContent = '조회 중...';
  btn.disabled = true;
  setProbeStatus('');

  try {
    const models = await probeModels({ providerType, baseUrl, apiKey, serverId });
    if (seq !== _probeSeq) return; // provider 전환/재조회/폼 닫기로 이 요청은 무효화됨

    const ids = models
      .map(m => m?.id)
      .filter(id => typeof id === 'string' && id.trim());
    const kept = meta.filterNonChatModels ? ids.filter(isChatCandidateModel) : ids;
    kept.sort((a, b) => a.localeCompare(b));

    if (!kept.length) {
      clearProbedModels();
      setProbeStatus(
        ids.length
          ? `${ids.length}개 모델을 받았지만 챗 모델이 없습니다. 모델명을 직접 입력하세요.`
          : '사용 가능한 모델이 없습니다. API 키와 주소를 확인하세요.',
        true,
      );
      return;
    }

    fillModelDatalist(kept);
    const hidden = ids.length - kept.length;
    setProbeStatus(
      `${kept.length}개 모델 조회됨` +
      (hidden > 0 ? ` (비챗 모델 ${hidden}개 숨김)` : '') +
      ' · 입력란을 클릭하면 목록이 나타납니다',
    );
    document.getElementById('sv-model').focus();
  } catch (e) {
    if (seq === _probeSeq) {
      clearProbedModels();
      setProbeStatus(e?.message || '모델 목록을 가져오지 못했습니다.', true);
    }
  } finally {
    // 버튼은 provider 가 바뀌어도 같은 DOM 엘리먼트를 재사용하므로 항상 복구한다.
    // (datalist/상태 문구만 seq 로 걸러내면 충분 — 그 외 UI 는 위에서 이미 가드됨)
    btn.textContent = original;
    btn.disabled = false;
  }
}

// 폼에서 마지막으로 적용된 provider. select 변경 시 "무엇에서 무엇으로" 를 알기 위해 유지한다.
let _formProviderType = DEFAULT_PROVIDER;
// openai 로 전환하며 thinking 을 강제로 끌 때, 되돌아올 경우를 위해 직전 상태를 보관한다.
let _thinkingBeforeDisable = null;

/**
 * 선택된 provider 에 맞게 폼 필드의 라벨/placeholder/활성 상태를 조정한다.
 * (값 자체는 건드리지 않는다 — 값 초기화는 onProviderChange 가 담당)
 */
function applyProviderFieldVisibility(providerType) {
  const meta = providerMeta(providerType);

  document.getElementById('sv-provider-hint').textContent = meta.hint;

  document.getElementById('sv-url-label').textContent = meta.urlLabel;
  document.getElementById('sv-url').placeholder        = meta.urlPlaceholder;

  document.getElementById('sv-api-key-label').textContent = meta.apiKeyLabel;
  // 편집 모드에서는 항상 빈 값으로 시작하므로(마스킹 값 prefill 금지) 안내 문구를 우선한다.
  document.getElementById('sv-api-key').placeholder = _editServerId
    ? '변경하려면 새 키 입력 (비워두면 기존 키 유지)'
    : meta.apiKeyPlaceholder;

  // 모델 목록 조회는 원격 프로바이더(openai/anthropic)에서만 의미가 있다.
  document.getElementById('sv-probe-btn')
    .classList.toggle('hidden', !meta.canProbeModels);

  const thinkingEl  = document.getElementById('sv-thinking');
  const thinkingRow = document.getElementById('sv-thinking-row');
  document.getElementById('sv-thinking-label').textContent = meta.thinkingLabel;

  if (!meta.thinkingEnabled) {
    if (!thinkingEl.disabled) _thinkingBeforeDisable = thinkingEl.checked;
    thinkingEl.checked  = false;
    thinkingEl.disabled = true;
    thinkingRow.classList.add('unsupported');
  } else {
    // 비활성화 때문에 꺼졌던 값이면 되돌려준다.
    if (thinkingEl.disabled && _thinkingBeforeDisable !== null) {
      thinkingEl.checked = _thinkingBeforeDisable;
    }
    _thinkingBeforeDisable = null;
    thinkingEl.disabled = false;
    thinkingRow.classList.remove('unsupported');
  }
}

/**
 * 로컬/사설망을 가리키는 주소인지 — provider 를 vLLM 에서 바꾼 뒤
 * 옛 vLLM 주소가 남아있는 상황을 저장 직전에 잡아내기 위한 휴리스틱.
 */
function isLikelyLocalUrl(raw) {
  let u;
  try { u = new URL(raw); } catch { return false; }
  if (u.protocol === 'http:') return true;
  const h = u.hostname;
  return h === 'localhost' || h === '::1' ||
         /^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h) ||
         /^172\.(1[6-9]|2\d|3[01])\./.test(h);
}

function onProviderChange() {
  const next = document.getElementById('sv-provider').value;
  const prev = _formProviderType;
  _formProviderType = next;
  applyProviderFieldVisibility(next);
  if (prev === next) return;

  // 이전 provider 의 모델 목록이 datalist 에 남아 헷갈리지 않도록 비우고,
  // 진행 중이던 조회 응답이 늦게 도착해도 이 결과를 덮어쓰지 못하게 무효화한다.
  _probeSeq++;
  clearProbedModels();

  // provider 가 바뀌면 base_url 의 의미도 바뀐다. 사용자가 입력한 값을 말없이 지우지 않고 확인받는다.
  const urlEl = document.getElementById('sv-url');
  const value = urlEl.value.trim();
  if (!value) return;

  const nextMeta = providerMeta(next);
  const fallback = nextMeta.defaultBaseUrl || '직접 입력한 주소';
  const ok = confirm(
    `Provider 를 ${providerMeta(prev).label} → ${nextMeta.label} 로 변경했습니다.\n` +
    `현재 Base URL(${value})은 새 Provider 에 맞지 않을 수 있습니다.\n\n` +
    `확인: 입력란을 비웁니다 (${fallback} 사용)\n` +
    `취소: 현재 값을 그대로 유지합니다`
  );
  if (ok) urlEl.value = '';
}

export async function openServerModal() {
  document.getElementById('server-modal').classList.remove('hidden');
  await loadServers();
}

function closeServerModal() {
  document.getElementById('server-modal').classList.add('hidden');
  hideServerForm();
}

async function loadServers() {
  try {
    _serverList = await api('GET', '/servers');
    renderServers();
  } catch (e) {
    document.getElementById('server-list').innerHTML =
      `<div class="server-empty">불러오기 실패: ${esc(e.message)}</div>`;
  }
}

function renderServers() {
  const listEl = document.getElementById('server-list');
  document.getElementById('server-count-badge').textContent = _serverList.length + '개';

  if (!_serverList.length) {
    listEl.innerHTML = '<div class="server-empty">등록된 서버가 없습니다.<br>+ 새 서버 버튼으로 추가하세요.</div>';
    return;
  }

  listEl.innerHTML = _serverList.map(s => {
    const ctxInfo       = s.model_len ? ` · ${(s.model_len / 1000).toFixed(0)}K ctx` : '';
    const modelShort    = s.model.split('/').pop();
    const providerType  = PROVIDERS[s.provider_type] ? s.provider_type : DEFAULT_PROVIDER;
    const providerMeta_ = PROVIDERS[providerType];
    const providerBadge =
      `<span class="server-provider-badge ${providerType}">${esc(providerMeta_.badge)}</span>`;
    const defaultBadge  = s.is_default ? '<span class="server-default-badge">기본</span>' : '';
    const thinkingBadge = s.thinking   ? '<span class="server-thinking-badge">Thinking</span>' : '';
    const statusBadge   = s.enabled
      ? '<span class="server-status enabled">활성</span>'
      : '<span class="server-status disabled">비활성</span>';
    // base_url 이 비어 있으면 프로바이더 기본 주소가 쓰인다는 걸 그대로 보여준다.
    const urlText = s.base_url
      ? `<div class="server-card-url">${esc(s.base_url)}</div>`
      : `<div class="server-card-url implicit">${esc(providerMeta_.defaultBaseUrl || '주소 미설정')}</div>`;
    return `
      <div class="server-card" data-id="${s.id}">
        <div class="server-card-top">
          <div class="server-card-info">
            <div class="server-card-name">${esc(s.name)} ${providerBadge} ${defaultBadge} ${thinkingBadge} ${statusBadge}</div>
            ${urlText}
            <div class="server-card-model">${esc(modelShort)}${esc(ctxInfo)}</div>
          </div>
          <div class="server-card-actions">
            <button class="server-health-btn" data-id="${s.id}">● 연결 확인</button>
            <button class="agent-edit-btn" data-edit="${s.id}">편집</button>
            <button class="agent-delete-btn" data-del="${s.id}">삭제</button>
          </div>
        </div>
      </div>`;
  }).join('');
}

// ── Health check ─────────────────────────────────────────────────────────────

// backend: GET /servers/{id}/health → { status, healthy, reachable, model_ok,
//                                       detail, available_models, ... }
const HEALTH_UI = {
  ok:            { label: '🟢 정상',      cls: 'healthy' },
  model_missing: { label: '🟡 모델 없음', cls: 'model-missing' },
  unreachable:   { label: '🔴 연결 실패', cls: 'unhealthy' },
  disabled:      { label: '⚪ 비활성',    cls: 'disabled' },
};
// status 필드가 없는 구버전 응답 / 미지의 status 값을 위한 폴백
const HEALTH_UI_FALLBACK = { label: '🔴 오류', cls: 'unhealthy' };

function resolveHealthUI(data) {
  const status = data?.status;
  const known = typeof status === 'string' && Object.hasOwn(HEALTH_UI, status)
    ? HEALTH_UI[status]
    : null;
  if (known) return known;
  // 구버전 응답(status 없음)이거나 프론트가 모르는 새 status → healthy 로 판단
  return data?.healthy === true ? HEALTH_UI.ok : HEALTH_UI_FALLBACK;
}

function buildHealthTitle(data) {
  const lines = [];
  if (data?.detail) lines.push(data.detail);

  // 모델을 확인하지 못한 경우에만 실제 모델 목록을 덧붙인다.
  // (detail 은 5개까지만 나열하므로 전체 목록은 여기서 보여준다)
  const models = Array.isArray(data?.available_models) ? data.available_models : [];
  if (data?.model_ok === false && models.length) {
    if (data?.model) lines.push(`설정된 모델: ${data.model}`);
    lines.push(`서버가 제공 중인 모델 (${models.length}개): ${models.join(', ')}`);
  }
  return lines.join('\n') || '상세 정보가 없습니다.';
}

async function checkServerHealth(serverId, btn) {
  if (btn.disabled) return;
  btn.textContent = '● 확인 중...';
  btn.disabled = true;
  btn.className = 'server-health-btn';
  btn.title     = '';
  try {
    const data = await api('GET', `/servers/${serverId}/health`);
    const ui = resolveHealthUI(data);
    btn.textContent = ui.label;
    btn.className   = 'server-health-btn ' + ui.cls;
    btn.title       = buildHealthTitle(data);
  } catch (e) {
    btn.textContent = HEALTH_UI_FALLBACK.label;
    btn.className   = 'server-health-btn ' + HEALTH_UI_FALLBACK.cls;
    btn.title       = '연결 확인 요청이 실패했습니다: ' + (e?.message || '알 수 없는 오류');
  } finally {
    btn.disabled = false;
  }
}

function showServerForm(serverId = null) {
  _editServerId = serverId;
  const server = serverId ? _serverList.find(s => s.id === serverId) : null;

  document.getElementById('server-form-title').textContent = server ? '서버 편집' : '새 서버';
  clearProbedModels();   // 이전 폼 세션의 조회 결과가 남지 않게 한다
  document.getElementById('sv-name').value    = server?.name ?? '';
  document.getElementById('sv-url').value     = server?.base_url ?? '';
  document.getElementById('sv-model').value   = server?.model ?? '';
  document.getElementById('sv-weight').value  = server?.weight ?? 1;
  // GET 응답의 api_key 는 마스킹된 표시용 문자열이다(sk-a...wxyz). 절대 prefill 하지 않는다.
  // 빈 값으로 저장하면 백엔드가 "변경 안 함" 으로 처리한다.
  document.getElementById('sv-api-key').value   = '';
  document.getElementById('sv-max-len').value   = server?.max_model_len ?? 0;
  document.getElementById('sv-default').checked  = server?.is_default ?? false;
  document.getElementById('sv-enabled').checked  = server?.enabled ?? true;
  document.getElementById('sv-enabled-row').style.display = serverId ? 'flex' : 'none';

  // thinking 은 provider 규칙 적용 전에 채워야 한다
  // (applyProviderFieldVisibility 가 지원하지 않는 provider 에서 강제로 끄기 때문)
  const providerType = PROVIDERS[server?.provider_type] ? server.provider_type : DEFAULT_PROVIDER;
  document.getElementById('sv-thinking').checked  = server?.thinking ?? false;
  document.getElementById('sv-thinking').disabled = false;
  _thinkingBeforeDisable = null;
  document.getElementById('sv-provider').value = providerType;
  _formProviderType = providerType;
  applyProviderFieldVisibility(providerType);

  document.getElementById('server-list-panel').classList.add('hidden');
  document.getElementById('server-form-panel').classList.remove('hidden');
  setTimeout(() => document.getElementById('sv-name').focus(), 50);
}

function hideServerForm() {
  _editServerId = null;
  _probeSeq++; // 폼을 닫은 뒤 도착하는 조회 응답이 다음 폼 세션에 반영되지 않게 한다
  document.getElementById('server-form-panel')?.classList.add('hidden');
  document.getElementById('server-list-panel')?.classList.remove('hidden');
}

async function saveServer() {
  const provider_type = document.getElementById('sv-provider').value;
  const name     = document.getElementById('sv-name').value.trim();
  const base_url = document.getElementById('sv-url').value.trim();
  const model    = document.getElementById('sv-model').value.trim();
  const meta     = providerMeta(provider_type);

  if (!name || !model) {
    alert('이름과 모델은 필수 항목입니다.');
    return;
  }
  // base_url 은 vLLM 에서만 필수 — openai/anthropic 은 비우면 기본 주소를 쓴다.
  if (meta.urlRequired && !base_url) {
    alert(`${meta.label} 서버는 Base URL 이 필수입니다.`);
    return;
  }
  // vLLM 주소를 남긴 채 provider 만 바꿔 저장하는 사고를 마지막으로 한 번 막는다.
  if (!meta.urlRequired && base_url && isLikelyLocalUrl(base_url)) {
    const ok = confirm(
      `Provider 는 ${meta.label} 인데 Base URL 이 로컬/사설망 주소입니다:\n${base_url}\n\n` +
      `프록시·게이트웨이 주소가 맞다면 "확인" 을 눌러 그대로 저장합니다.\n` +
      `"취소" 를 누르면 저장하지 않으니 주소를 비우거나 수정하세요 ` +
      `(비우면 ${meta.defaultBaseUrl} 사용).`
    );
    if (!ok) return;
  }

  const body = {
    provider_type,
    name, base_url, model,
    api_key:       document.getElementById('sv-api-key').value.trim(),
    weight:        parseInt(document.getElementById('sv-weight').value) || 1,
    max_model_len: parseInt(document.getElementById('sv-max-len').value) || 0,
    is_default:    document.getElementById('sv-default').checked,
    thinking:      document.getElementById('sv-thinking').checked,
  };
  if (_editServerId) body.enabled = document.getElementById('sv-enabled').checked;

  try {
    if (_editServerId) await api('PUT',  `/servers/${_editServerId}`, body);
    else               await api('POST', '/servers', body);
    hideServerForm();
    await loadServers();
    await loadModelStatus();
  } catch (e) {
    alert('저장 실패: ' + e.message);
  }
}

async function deleteServer(id) {
  const server = _serverList.find(s => s.id === id);
  if (!confirm(`"${server?.name}" 서버를 삭제할까요?`)) return;
  try {
    await api('DELETE', `/servers/${id}`);
    await loadServers();
    await loadModelStatus();
  } catch (e) {
    alert('삭제 실패: ' + e.message);
  }
}

// ── Init (event bindings) ────────────────────────────────────────────────────

export function initServerEvents() {
  document.getElementById('server-selector-btn').addEventListener('click', e => {
    e.stopPropagation();
    toggleServerDropdown();
  });
  document.addEventListener('click', e => {
    if (!document.getElementById('server-selector').contains(e.target)) {
      closeServerDropdown();
    }
  });

  // 정적 컨테이너에 위임 바인딩을 1회만 등록한다.
  // (렌더 함수 안에서 등록하면 재렌더마다 리스너가 누적되어 1클릭에 N번 실행된다)
  document.getElementById('server-dropdown-list').addEventListener('click', async e => {
    const item = e.target.closest('.server-dropdown-item');
    if (item) await switchDefaultServer(item.dataset.id);
  });
  document.getElementById('server-list').addEventListener('click', async e => {
    const healthBtn = e.target.closest('.server-health-btn');
    const editBtn   = e.target.closest('[data-edit]');
    const delBtn    = e.target.closest('[data-del]');
    if (healthBtn) await checkServerHealth(healthBtn.dataset.id, healthBtn);
    if (editBtn)   showServerForm(editBtn.dataset.edit);
    if (delBtn)    await deleteServer(delBtn.dataset.del);
  });

  document.getElementById('server-btn').onclick        = openServerModal;
  document.getElementById('server-close').onclick      = closeServerModal;
  document.getElementById('server-new-btn').onclick    = () => showServerForm();
  document.getElementById('server-form-close').onclick = hideServerForm;
  document.getElementById('server-form-cancel').onclick = hideServerForm;
  document.getElementById('server-form-save').onclick  = saveServer;
  document.getElementById('sv-provider').addEventListener('change', onProviderChange);
  document.getElementById('sv-probe-btn').addEventListener('click', onProbeModelsClick);

  document.getElementById('server-modal').addEventListener('click', e => {
    if (e.target === document.getElementById('server-modal')) closeServerModal();
  });
}
