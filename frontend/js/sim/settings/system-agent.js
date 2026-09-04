// frontend/js/sim/settings/system-agent.js
// system(내레이터) 에이전트 설정 렌더링.

import { sim } from '../state.js';

export function renderSystemAgentConfig() {
  const sa      = sim.system_agent || {};
  const enabled = !!sa.enabled;
  const chk     = document.getElementById('sim-sys-enabled');
  const cfg     = document.getElementById('sim-sys-config');
  if (!chk || !cfg) return;

  chk.checked = enabled;
  cfg.classList.toggle('sim-hidden', !enabled);

  document.getElementById('sim-sys-icon').value          = sa.icon                  || '🎬';
  document.getElementById('sim-sys-display-name').value  = sa.display_name          || '내레이터';
  document.getElementById('sim-sys-prompt').value        = sa.system_prompt         || '';
  document.getElementById('sim-sys-interval').value      = sa.intervention_interval ?? 1;
  document.getElementById('sim-sys-silence').value       = sa.silence_threshold     ?? 3;
  const digestEl = document.getElementById('sim-sys-digest');
  if (digestEl) digestEl.value = sa.digest_waves ?? 6;
  document.getElementById('sim-sys-director-note').value = sa.director_note         || '';

  chk.onchange = () => {
    sim.system_agent.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
  };
}
