// frontend/js/sim/settings/temperature.js
// 샘플링 온도 슬라이더.

import { sim, normalizeTemperature } from '../state.js';
import { refreshAgentTempPlaceholders } from './agents.js';

// 시뮬레이션 전체 기본 temperature. 에이전트 카드의 온도 입력은 이 값을 상속하므로,
// 슬라이더를 움직이면 카드들의 placeholder("기본값 0.7")도 같이 갱신한다.
export function renderTemperatureSlider() {
  const slider = document.getElementById('sim-temperature');
  const valEl  = document.getElementById('sim-temperature-val');
  if (!slider) return;

  const temp = normalizeTemperature(sim.temperature);
  sim.temperature = temp;              // 구버전 시나리오의 범위 밖 값을 상태에서도 바로잡는다
  slider.value = String(temp);
  if (valEl) valEl.textContent = temp.toFixed(1);

  // renderSettingsPage()는 여러 번 호출되므로 addEventListener 대신 oninput으로
  // 덮어써서 리스너가 중복 등록되지 않게 한다 (renderServerSelect의 onchange와 동일한 규칙).
  slider.oninput = () => {
    sim.temperature = normalizeTemperature(slider.value);
    if (valEl) valEl.textContent = sim.temperature.toFixed(1);
    refreshAgentTempPlaceholders();
  };
}
