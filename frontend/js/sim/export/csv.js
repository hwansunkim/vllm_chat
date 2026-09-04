// frontend/js/sim/export/csv.js
// 위치 이력 CSV 내보내기 — 감염병 접촉 분석용(누가 언제 어느 장소에 있었는가).
//
// 데이터 출처는 마크다운 내보내기와 같은 `GET /api/simulation/logs` 다. 각 로그 항목은
// "그 에이전트가 그 wave에 턴을 받았다"는 사실이고, 엔진이 실어주는 `location`/`is_exterior`
// 는 **그 wave의 이동이 적용되기 전** 값 — 즉 그 wave 동안 실제로 접촉이 일어난 장소다
// (ABM/simulation/turn.py `_apply_turn_result` 참고).
//
// 구버전 run(컬럼 추가 이전)의 로그는 `location`/`is_exterior`가 null/없음이다. 그 행을
// 버리지 않고 빈 값으로 남긴다 — wave/agent 정보 자체는 유효하기 때문.
//
// `wave_end_time`은 `GET /api/simulation/events?types=time_jump` 의 `data.end_time_str`
// (엔진이 그 wave의 시간 델타를 적용한 뒤의 절대 시각, ABM/simulation/runner.py)을 1순위로
// 쓴다. 이 이벤트는 **가변 시간 모드에서만**, 그리고 강제 침묵 재투입 wave를 빼고 emit되므로
// 고정 시간 모드·침묵 wave·이 필드 추가 이전에 저장된 run에는 없다. 그래서 기존의
// "다음 wave의 시작 시각 훔쳐보기" 폴백을 그대로 남긴다(마지막 wave만 빈칸이 되던 그 로직).

import { sim } from '../state.js';
import { downloadFile, safeFilename, nowTag } from '../utils/download.js';

const CSV_HEADER = ['wave', 'wave_start_time', 'wave_end_time', 'agent', 'location', 'is_exterior'];

// CSV 필드 이스케이프 (RFC 4180): 쉼표·따옴표·개행이 있으면 따옴표로 감싸고 내부 `"`는 `""`.
function csvField(value) {
  if (value === null || value === undefined) return '';
  const s = String(value);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function csvRow(values) {
  return values.map(csvField).join(',');
}

/**
 * `time_jump` 이벤트 배열 → wave → end_time_str 맵.
 *
 * 이벤트의 `wave`는 표시 wave(disp_wave — /continue 의 wave_base가 반영된 값)라 턴 로그의
 * `wave`와 같은 좌표계다. `end_time_str`이 없는(=이 필드 추가 이전에 저장된) 이벤트는
 * 건너뛰어 폴백이 그 wave를 처리하게 둔다.
 *
 * @param {Array<Object>} events - `[{wave, event_type, timestamp, data: {end_time_str, ...}}]`
 * @returns {Map<number, string>}
 */
function buildWaveEndMap(events) {
  const map = new Map();
  for (const evt of (events || [])) {
    if (!evt || typeof evt !== 'object') continue;
    const end = evt.data?.end_time_str;
    if (typeof end !== 'string' || !end) continue;
    // 한 wave에 이벤트가 두 번 실린 경우(재개 등)엔 나중 값이 최신이므로 덮어쓴다.
    map.set(evt.wave ?? 0, end);
  }
  return map;
}

/**
 * 로그 배열 → 위치 이력 CSV 문자열. 순수 함수(네트워크 접근 없음).
 *
 * @param {Array<Object>} log - `/api/simulation/logs` 응답 (항목: {speaker, wave, time_str, location, is_exterior}).
 * @param {Array<Object>} [timeJumpEvents] - `/api/simulation/events?types=time_jump` 응답.
 *   생략/빈 배열이면 `wave_end_time`은 예전처럼 "다음 wave 시작 시각" 폴백으로만 채워진다.
 * @returns {string} CRLF 줄바꿈 CSV (헤더 1행 + 로그 항목당 1행).
 */
export function buildLocationCsv(log, timeJumpEvents) {
  const entries = (log || []).filter(e => e && typeof e === 'object');

  // 1) wave → time_str. 같은 wave의 모든 턴은 같은 time_str을 공유하므로 처음 만난 값이면 충분하다.
  //    (앞쪽 항목에 time_str이 없을 수 있어 wave의 첫 항목만 보지 않고 전체를 훑는다.)
  const waveStart = new Map();
  for (const e of entries) {
    const w = e.wave ?? 0;
    if (!waveStart.has(w) && e.time_str) waveStart.set(w, e.time_str);
  }

  // 2) 각 wave의 종료 시각. 우선순위:
  //      (a) time_jump 이벤트의 end_time_str  — 마지막 wave도 채워지는 유일한 경로
  //      (b) 다음 wave의 시작 시각             — 고정 시간 모드/침묵 wave/구버전 run 폴백
  //      (c) 빈 문자열
  //    (a)와 (b)는 엔진 불변식상 같은 값이므로 섞여도 열 값이 흔들리지 않는다.
  const jumpEnd = buildWaveEndMap(timeJumpEvents);
  const waves = [...new Set(entries.map(e => e.wave ?? 0))].sort((a, b) => a - b);
  const waveEnd = new Map();
  waves.forEach((w, i) => {
    const next = waves[i + 1];
    const fallback = next === undefined ? '' : (waveStart.get(next) ?? '');
    waveEnd.set(w, jumpEnd.get(w) ?? fallback);
  });

  // 3) wave 오름차순 정렬(같은 wave 안에서는 원래 턴 순서 유지 — Array.sort는 stable).
  const ordered = entries.slice().sort((a, b) => (a.wave ?? 0) - (b.wave ?? 0));

  const lines = [csvRow(CSV_HEADER)];
  for (const e of ordered) {
    const w = e.wave ?? 0;
    lines.push(csvRow([
      w,
      waveStart.get(w) ?? '',
      waveEnd.get(w) ?? '',
      e.speaker ?? '',
      e.location ?? '',                                        // 구버전 로그 → ''
      typeof e.is_exterior === 'boolean' ? String(e.is_exterior) : '',  // true / false / ''
    ]));
  }
  return lines.join('\r\n') + '\r\n';
}

/**
 * 현재 실행의 위치 이력을 CSV 파일로 다운로드한다.
 * 로그가 비어 있으면 다운로드하지 않고 알림만 띄운다.
 */
export async function exportLocationHistoryCsv() {
  // 두 요청은 서로 독립이라 병렬로 던진다(markdown.js `fetchAll()` 과 같은 패턴).
  // 이벤트 조회는 실패해도 CSV를 막지 않는다 — wave_end_time 이 폴백 경로로 내려갈 뿐이다.
  const [logRes, evtRes] = await Promise.all([
    fetch('/api/simulation/logs'),
    fetch('/api/simulation/events?types=time_jump').catch(() => null),
  ]);

  if (!logRes.ok) throw new Error(`로그 조회 실패 (HTTP ${logRes.status})`);
  const log = await logRes.json();

  if (!Array.isArray(log) || !log.length) {
    alert('내보낼 로그가 없습니다. 시뮬레이션을 먼저 실행하세요.');
    return;
  }

  let timeJumpEvents = [];
  if (evtRes?.ok) {
    try {
      const parsed = await evtRes.json();
      if (Array.isArray(parsed)) timeJumpEvents = parsed;
    } catch (e) {
      console.warn('[export] time_jump 이벤트 파싱 실패 — 폴백으로 진행합니다.', e);
    }
  }

  const scenarioName = sim.currentScenarioName || '시나리오';
  const filename = safeFilename(`${scenarioName}_${nowTag()}`) + '_locations.csv';
  downloadFile(buildLocationCsv(log, timeJumpEvents), filename, 'text/csv;charset=utf-8');
}
