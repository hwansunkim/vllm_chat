# 프론트엔드 완료 — 시뮬레이션 실행 이력 UI

## 변경 파일 목록

1. `/home/wskim/work/vllm_chat/frontend/js/simulation.js`
2. `/home/wskim/work/vllm_chat/frontend/index.html`
3. `/home/wskim/work/vllm_chat/frontend/css/simulation.css`

## 주요 변경 내용

### simulation.js

- **startSimulation()** (약 522줄): `/api/simulation/start` 요청 body에 `scenario_id: sim.currentScenarioId || null` 추가
- **loadScenarios()** (약 575줄): 함수 끝에 `sim-history-btn` 활성화/비활성화 로직 추가
- **toggleRunHistory()** (신규): 이력 패널 토글. 패널이 hidden이면 표시 후 refreshRunHistory() 호출, 표시 중이면 숨김
- **refreshRunHistory()** (신규): `GET /api/simulation/runs?scenario_id=...` 호출, 결과를 테이블로 렌더링, 삭제 버튼 이벤트 바인딩 (DELETE /api/simulation/runs/{run_id})
- **applyScenario()** (약 730줄): renderSettingsPage() 호출 전에 histBtn 활성화 + textContent 리셋, refreshRunHistory() 호출 추가
- **initSimulationEvents()** (약 1154줄): `sim-history-btn` 클릭 이벤트 → toggleRunHistory 바인딩 추가

### index.html

- `sim-settings-hdr-controls` 내 `🗑 삭제` 버튼 다음에 `<button id="sim-history-btn" class="sim-ctrl-btn history" disabled>📋 이력</button>` 추가
- `sim-settings-header` 닫는 태그 다음에 `<div id="sim-run-history-panel" class="sim-hidden"></div>` 추가 (이벤트 리스너는 simulation.js initSimulationEvents()에서 처리)

### simulation.css

- `.sim-ctrl-btn.history` 및 `:hover:not(:disabled)` 스타일 추가 (파란 계열)
- `#sim-run-history-panel` 레이아웃 스타일
- `.sim-run-history-empty`, `.sim-run-history-table`, `.rh-*` 셀 스타일
- `.sim-run-del-btn` 및 `:hover` 스타일

## API 의존성

- `GET /api/simulation/runs?scenario_id={id}` — runs 배열 반환 (백엔드 구현 필요)
- `DELETE /api/simulation/runs/{run_id}` — 204 No Content (백엔드 구현 필요)
- `POST /api/simulation/start` body에 `scenario_id` 필드 수신 (백엔드 처리 필요)
