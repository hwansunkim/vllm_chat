// frontend/js/sim/settings/early-stop.js
// 조기 종료 토글 UI 연동 — 꺼져 있으면 "최대 침묵 wave" 입력을 비활성화한다.

import { sim } from '../state.js';

export function updateEarlyStopUI(enabled) {
  const silenceInput = document.getElementById('sim-max-silence-waves');
  if (silenceInput) silenceInput.disabled = !enabled;
}

export function initEarlyStopToggle() {
  const chk = document.getElementById('sim-early-stop-enabled');
  if (!chk) return;
  chk.onchange = () => {
    sim.early_stop_enabled = chk.checked;
    updateEarlyStopUI(chk.checked);
  };
}
