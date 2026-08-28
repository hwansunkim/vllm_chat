// frontend/js/sim/settings/target-duration.js
// 목표 기간 (숫자 + 단위 ↔ target_duration_minutes) 폼 연동.

import { sim, DEFAULT_DURATION_UNIT, normalizeTargetDuration,
         minutesToDurationParts, durationPartsToMinutes,
         isTimeConceptDisabled } from '../state.js';

// 마지막으로 폼에 그려 넣은 (숫자, 단위)와 그때의 원본 분 값.
// 저장된 분 값이 어떤 단위로도 딱 떨어지지 않으면 화면에는 일 단위 근사치를 보여주는데,
// 사용자가 입력을 건드리지 않았다면 이 원본 분 값을 그대로 되돌려줘야 한다.
// (근사치를 다시 저장하면 저장할 때마다 값이 조금씩 흘러가 버린다.)
let _renderedDuration = { value: '', unit: DEFAULT_DURATION_UNIT, minutes: null, exact: true };

export function renderTargetDuration() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return;

  const minutes = normalizeTargetDuration(sim.target_duration_minutes);
  sim.target_duration_minutes = minutes;      // 구버전/잘못된 값(0, 음수, 문자열)을 상태에서도 바로잡는다
  const parts = minutesToDurationParts(minutes);
  valEl.value  = parts.value === '' ? '' : String(parts.value);
  unitEl.value = parts.unit;
  _renderedDuration = { value: valEl.value, unit: unitEl.value, minutes, exact: parts.exact };

  updateTargetDurationUI();
}

/**
 * 폼이 렌더 직후 그대로면 원본 분 값, 사용자가 건드렸으면 입력값 × 단위.
 * (상태를 바꾸지 않는 조회 전용 — 힌트 계산과 실제 수집이 같은 값을 보게 한다.)
 */
function _resolveTargetMinutes() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return normalizeTargetDuration(sim.target_duration_minutes);
  const raw = String(valEl.value).trim();
  if (raw === _renderedDuration.value && unitEl.value === _renderedDuration.unit) {
    return _renderedDuration.minutes;
  }
  return durationPartsToMinutes(raw, unitEl.value);
}

/**
 * 폼 → target_duration_minutes.
 * 렌더 직후 그대로면 원본 분 값을 반환(근사 표시로 인한 값 유실 방지),
 * 사용자가 숫자나 단위를 바꿨으면 입력값 × 단위로 새로 환산한다.
 */
export function readTargetDuration() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  // 설정 패널이 아직 렌더되지 않은 경로에서는 기존 상태를 그대로 유지한다 (temperature와 같은 규칙).
  if (!valEl || !unitEl) return normalizeTargetDuration(sim.target_duration_minutes);

  const minutes = _resolveTargetMinutes();
  const raw     = String(valEl.value).trim();
  // 사용자가 직접 입력한 값이 되었으므로 이후로는 그 값이 정확한 기준이 된다.
  if (raw !== _renderedDuration.value || unitEl.value !== _renderedDuration.unit) {
    _renderedDuration = { value: raw, unit: unitEl.value, minutes, exact: true };
  }
  return minutes;
}

/** 현재 폼 상태 기준으로 목표 기간 입력의 활성/비활성과 안내 문구를 갱신. */
export function updateTargetDurationUI() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  const hintEl = document.getElementById('sim-target-duration-hint');
  if (!valEl || !unitEl || !hintEl) return;

  // 시간 모드/wave당 시간은 폼에서 실시간으로 읽는다 — 아직 sim에 반영되기 전일 수 있다.
  const mode = document.getElementById('sim-time-mode')?.value === 'variable' ? 'variable' : 'fixed';
  const tpw  = parseInt(document.getElementById('sim-time-per-wave')?.value) || 0;
  const off  = isTimeConceptDisabled(mode, tpw);

  valEl.disabled  = off;
  unitEl.disabled = off;
  hintEl.classList.remove('sim-hint-warn', 'sim-hint-off');

  if (off) {
    hintEl.classList.add('sim-hint-off');
    hintEl.textContent = '시간 개념이 꺼져 있어 사용할 수 없습니다 (시간 모드 "고정" + wave당 시간 0분).';
    return;
  }

  const minutes = _resolveTargetMinutes();
  if (minutes === null) {
    hintEl.textContent = '비워두면 목표 기간 없이 최대 wave 수까지 실행합니다.';
    return;
  }

  // 저장된 분 값이 어떤 단위로도 딱 떨어지지 않아 근사치로 표시 중이면 그 사실을 밝힌다
  // (실제로 저장/전송되는 값은 아래 "= N분"이며 입력을 건드리지 않는 한 그대로 유지된다).
  const approx = !_renderedDuration.exact &&
                 String(valEl.value).trim() === _renderedDuration.value &&
                 unitEl.value === _renderedDuration.unit;
  let text = `= ${minutes.toLocaleString('ko-KR')}분${approx ? '(표시는 근사치)' : ''}`
           + ' · 이번 실행에서 이만큼 진행되면 종료됩니다';
  // 고정 모드에서는 필요한 wave 수가 결정론적으로 계산된다 — 안전장치(max_waves)가
  // 먼저 걸리면 목표 기간에 닿지 못하므로 미리 알려준다.
  if (mode === 'fixed' && tpw > 0) {
    const needed   = Math.ceil(minutes / tpw);
    const maxWaves = parseInt(document.getElementById('sim-max-waves')?.value) || 10;
    text += ` · 약 ${needed.toLocaleString('ko-KR')} wave 필요`;
    if (needed > maxWaves) {
      hintEl.classList.add('sim-hint-warn');
      text += ` — 최대 wave 수(${maxWaves})에서 먼저 종료됩니다`;
    }
  }
  hintEl.textContent = text;
}

export function initTargetDurationUI() {
  const valEl  = document.getElementById('sim-target-duration-value');
  const unitEl = document.getElementById('sim-target-duration-unit');
  if (!valEl || !unitEl) return;

  const sync = () => {
    sim.target_duration_minutes = readTargetDuration();
    updateTargetDurationUI();
  };
  valEl.addEventListener('input',  sync);
  unitEl.addEventListener('change', sync);
  // 시간 모드/wave당 시간/최대 wave 수는 목표 기간의 활성 여부와 안내 문구에 영향을 준다.
  document.getElementById('sim-time-per-wave')?.addEventListener('input', updateTargetDurationUI);
  document.getElementById('sim-max-waves')?.addEventListener('input', updateTargetDurationUI);
}
