export async function api(method, path, body) {
  const res = await fetch('/api' + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const msg = await res.text();
    throw new Error(msg || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

/**
 * POST /api/servers/probe-models — 등록 전에 실제 사용 가능한 모델 목록을 조회한다.
 * 공용 api() 를 쓰지 않는 이유: 실패 시 body 를 raw text 로 던지므로
 * 400 의 사람이 읽을 detail 과 422 의 배열 detail 을 구분할 수 없다.
 *
 * serverId 를 넘기면(편집 모드), apiKey 가 비어 있을 때 백엔드가 그 서버의
 * 저장된 키로 대신 조회한다 — 편집 화면에서 마스킹 때문에 키를 다시 입력하지
 * 않아도 "모델 불러오기"가 동작하게 하기 위함.
 *
 * @returns {Promise<{id: string, context_length: number|null}[]>}
 */
export async function probeModels({ providerType, baseUrl, apiKey, serverId }) {
  const res = await fetch('/api/servers/probe-models', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider_type: providerType,
      base_url: baseUrl || '',
      api_key: apiKey || '',
      server_id: serverId || null,
    }),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => null);
    const detail = err?.detail;
    // 400 → detail 은 그대로 노출 가능한 한국어 문장.
    // 422(배열) 등 그 외 형태는 폴백 문구를 쓴다.
    const msg = typeof detail === 'string' && detail.trim()
      ? detail
      : `모델 목록을 가져오지 못했습니다. (HTTP ${res.status})`;
    throw new Error(msg);
  }

  const data = await res.json();
  return Array.isArray(data?.models) ? data.models : [];
}
