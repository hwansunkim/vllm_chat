// frontend/js/sim/settings/infection-config.js
// 감염병 모델 설정 — 폼 ↔ sim.infection_model 동기화. 서버 검증(확률 0~1,
// max_waves >= min_waves)을 통과하도록 읽어들이는 모든 경로가 buildInfectionModel()을 거친다.

import { sim, esc, buildInfectionModel, normalizeProbability,
         normalizeSymptomStages } from '../state.js';
import { renderScenarioEvents } from './events.js';

export function renderInfectionConfig() {
  const model = sim.infection_model = buildInfectionModel(sim.infection_model);
  const chk   = document.getElementById('sim-inf-enabled');
  const cfg   = document.getElementById('sim-inf-config');
  if (!chk || !cfg) return;

  chk.checked = model.enabled;
  cfg.classList.toggle('sim-hidden', !model.enabled);

  const nameEl = document.getElementById('sim-inf-disease-name');
  if (nameEl) nameEl.value = model.disease_name;
  const immuneEl = document.getElementById('sim-inf-immune');
  if (immuneEl) immuneEl.value = model.immune_after_recovery ? 'sir' : 'sis';

  _bindProbabilitySlider('sim-inf-transmission', 'transmission_probability');
  _bindProbabilitySlider('sim-inf-recovery',     'recovery_probability');
  _renderSymptomStages();

  // renderSettingsPage()는 여러 번 호출되므로 addEventListener 대신 onchange로 덮어쓴다
  // (renderSystemAgentConfig / renderServerSelect와 같은 규칙 — 리스너 중복 등록 방지).
  chk.onchange = () => {
    sim.infection_model.enabled = chk.checked;
    cfg.classList.toggle('sim-hidden', !chk.checked);
    // infect_agent 이벤트는 모델이 꺼져 있으면 서버에서 무시된다 —
    // 이벤트 에디터의 경고 배지를 지금 상태에 맞춰 다시 그린다.
    renderScenarioEvents();
  };
  if (nameEl)   nameEl.oninput    = () => { sim.infection_model.disease_name = nameEl.value.trim(); };
  if (immuneEl) immuneEl.onchange = () => { sim.infection_model.immune_after_recovery = immuneEl.value !== 'sis'; };
}

/** 확률 슬라이더 + 현재 값 표시. 값은 즉시 상태에 반영된다(temperature 슬라이더와 동일). */
function _bindProbabilitySlider(id, field) {
  const slider = document.getElementById(id);
  const valEl  = document.getElementById(`${id}-val`);
  if (!slider) return;
  const paint = () => { if (valEl) valEl.textContent = sim.infection_model[field].toFixed(2); };
  slider.value = String(sim.infection_model[field]);
  paint();
  slider.oninput = () => {
    sim.infection_model[field] = normalizeProbability(slider.value, sim.infection_model[field]);
    paint();
  };
}

// 증상 단계 에디터 — time_categories 에디터와 같은 구조(id/label/min/max)에
// symptom_text textarea를 더한 형태. 단위는 분이 아니라 **웨이브**다.
function _renderSymptomStages() {
  const container = document.getElementById('sim-inf-stages');
  if (!container) return;
  const stages = sim.infection_model.symptom_stages;
  container.innerHTML = '';

  if (!stages.length) {
    const empty = document.createElement('div');
    empty.className = 'sim-inf-stage-empty';
    empty.textContent = '증상 단계가 없습니다 — 단계를 추가해야 에이전트가 자기 몸 상태를 인지합니다.';
    container.appendChild(empty);
  }

  stages.forEach((stage, idx) => {
    const row = document.createElement('div');
    row.className = 'sim-inf-stage-row';
    row.dataset.stageId = stage.id;
    row.innerHTML = `
      <div class="sim-inf-stage-top">
        <input type="text" class="sim-inf-stage-label" data-idx="${idx}"
               value="${esc(stage.label)}" placeholder="단계 이름"/>
        <input type="number" class="sim-inf-stage-min" data-idx="${idx}"
               min="0" max="999" value="${stage.min_waves}"/>
        <span class="sim-inf-stage-sep">~</span>
        <input type="number" class="sim-inf-stage-max" data-idx="${idx}"
               min="0" max="999" value="${stage.max_waves}"/>
        <span class="sim-inf-stage-unit">웨이브</span>
        <button class="sim-inf-stage-del" data-idx="${idx}" title="단계 삭제">×</button>
      </div>
      <textarea class="sim-inf-stage-text" data-idx="${idx}" rows="2"
                placeholder="이 단계에서 에이전트가 느끼는 몸 상태를 서술하세요. 이 문장이 LLM에게 전달되는 유일한 정보입니다.">${esc(stage.symptom_text)}</textarea>`;
    container.appendChild(row);
  });

  // 입력은 즉시 상태에 반영한다 — 삭제/추가로 다시 그릴 때 편집 중이던 값이 날아가지 않게.
  container.oninput = e => {
    const el  = e.target;
    const idx = parseInt(el.dataset?.idx);
    const stage = sim.infection_model.symptom_stages[idx];
    if (!stage) return;
    if (el.classList.contains('sim-inf-stage-label'))     stage.label        = el.value;
    else if (el.classList.contains('sim-inf-stage-text')) stage.symptom_text = el.value;
    else if (el.classList.contains('sim-inf-stage-min'))  stage.min_waves    = el.value;
    else if (el.classList.contains('sim-inf-stage-max'))  stage.max_waves    = el.value;
  };
  // min/max는 포커스를 벗어날 때(change) 정규화해서 다시 그린다 — 그래야 사용자가
  // 방금 고친 값이 실제로 clamp된 뒤의 값과 화면에서 어긋나지 않는다. 매 키 입력마다
  // (oninput) 다시 그리면 타이핑 중간에 커서가 날아가므로 change 시점에만 한다.
  container.onchange = e => {
    const el = e.target;
    if (!el.classList.contains('sim-inf-stage-min') && !el.classList.contains('sim-inf-stage-max')) return;
    sim.infection_model.symptom_stages = normalizeSymptomStages(sim.infection_model.symptom_stages);
    _renderSymptomStages();
  };
  container.onclick = e => {
    const del = e.target.closest('.sim-inf-stage-del');
    if (!del) return;
    sim.infection_model.symptom_stages.splice(parseInt(del.dataset.idx), 1);
    _renderSymptomStages();
  };
}

/** "+ 단계 추가" 버튼 — 마지막 단계 다음 웨이브부터 시작하는 빈 단계를 붙인다. */
export function addSymptomStage() {
  sim.infection_model = buildInfectionModel(sim.infection_model);
  const stages = sim.infection_model.symptom_stages;
  const last   = stages[stages.length - 1];
  const start  = last ? last.max_waves + 1 : 0;
  stages.push({
    id:           `stage${stages.length + 1}`,
    label:        `단계 ${stages.length + 1}`,
    min_waves:    start,
    max_waves:    start + 2,
    symptom_text: '',
  });
  _renderSymptomStages();
}

/** 폼 → infection_model. 설정 패널이 렌더되지 않은 경로에서는 기존 상태를 유지한다. */
export function readInfectionModel() {
  const chk = document.getElementById('sim-inf-enabled');
  if (!chk) return buildInfectionModel(sim.infection_model);
  return buildInfectionModel({
    enabled:                  chk.checked,
    disease_name:             document.getElementById('sim-inf-disease-name')?.value ?? '',
    transmission_probability: document.getElementById('sim-inf-transmission')?.value,
    recovery_probability:     document.getElementById('sim-inf-recovery')?.value,
    // 편집기가 비어 있으면 빈 배열 그대로 — 백엔드도 빈 목록을 허용한다(증상 없음).
    symptom_stages:           sim.infection_model?.symptom_stages ?? [],
    immune_after_recovery:    document.getElementById('sim-inf-immune')?.value !== 'sis',
  });
}
