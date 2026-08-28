// frontend/js/sim/settings/server-select.js
// 시뮬레이션 레벨 서버 드롭다운. 목록 자체는 server-list.js의 공유 캐시에서 온다.

import { sim } from '../state.js';
import { getServerList } from './server-list.js';

export async function renderServerSelect() {
  const sel  = document.getElementById('sim-server-select');
  const hint = document.getElementById('sim-server-hint');
  if (!sel) return;

  const servers = await getServerList();

  sel.innerHTML = `<option value="">기본 서버</option>` +
    servers.map(s =>
      `<option value="${s.id}">${s.name}</option>`
    ).join('');

  // 저장된 server_id로 선택값 복원
  sel.value = sim.server_id || '';

  updateServerHint(sel.value, servers);

  sel.onchange = () => {
    sim.server_id = sel.value || null;
    updateServerHint(sel.value, servers);
  };
}

function updateServerHint(serverId, servers) {
  const hint = document.getElementById('sim-server-hint');
  if (!hint) return;
  if (!serverId) {
    const def = servers.find(s => s.is_default);
    hint.textContent = def
      ? `기본: ${def.model.split('/').pop()} · ${def.base_url}`
      : '등록된 서버가 없으면 환경변수 설정을 사용합니다';
  } else {
    const s = servers.find(s => s.id === serverId);
    hint.textContent = s ? `${s.model.split('/').pop()} · ${s.base_url}` : '';
  }
}
