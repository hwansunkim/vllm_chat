// frontend/js/sim/runs/wave-context.js
// 이력 피드(이력 보기 모달 · 불러온 피드)의 "누가 그 wave 에 어디서 어떤 상태였나" 조회.
//
// 실행 중에는 엔진이 주입하는 씬 알림(위치 안내·상태 해제 등)으로 보이던 정보가 이력에는
// 대사만 남아 빠진다. 저장된 이벤트로 되살린다(엔진·DB 변경 없음):
//   - wave_roster: wave 마다 전원의 **wave 시작** 위치·감염 상태·이동 중 여부(감염 판정과 같은 기준)
//   - agent_status_change: 수면·업무 같은 상태 진입(enter)·해제(clear)
// DOM·state.js 에 기대지 않는 순수 모듈이다(node 로 단위 테스트한다).

/** 이력 피드가 대사·채널 이벤트와 함께 받아오는 문맥 이벤트 타입. wave_roster 는 명시 요청해야 온다. */
export const CONTEXT_EVENT_TYPES = ['wave_roster', 'agent_status_change'];

/**
 * 저장 이벤트 → wave·에이전트별 문맥 조회기.
 *
 * `at(wave, agent)` → `{ location, infection, traveling, activity }` 또는 null(그 wave 기록 없음 —
 * wave_roster 가 없는 구버전 실행). `activity` 는 `{ state, label }` 또는 null.
 *
 * 상태 구간: 진입은 턴 안에서 일어나므로 **다음 wave 부터**, 해제는 wave 시작(자연 만료)에
 * 일어나므로 **그 wave 부터** 상태가 아니다 → enter@w … clear@c 는 [w+1, c). 이동 중(traveling)은
 * roster 의 traveling 으로 보이므로 여기서는 뺀다.
 */
export function buildWaveContext(events) {
  const roster = new Map();        // wave → Map(agent → row)
  const intervals = new Map();     // agent → [{ from, to, state, label }]
  const open = new Map();          // agent → 아직 안 끝난 구간

  const sorted = (events || [])
    .filter(e => e && CONTEXT_EVENT_TYPES.includes(e.event_type))
    .map((e, i) => ({ e, i, wave: e.wave ?? e.data?.wave ?? 0 }))
    .sort((a, b) => (a.wave - b.wave) || (a.i - b.i));

  for (const { e, wave } of sorted) {
    const d = e.data || {};
    if (e.event_type === 'wave_roster') {
      const rows = new Map();
      for (const r of d.agents || []) if (r && r.agent) rows.set(r.agent, r);
      roster.set(wave, rows);
      continue;
    }
    if (!d.agent || d.state === 'traveling') continue;
    const cur = open.get(d.agent);
    if (cur) { cur.to = d.action === 'clear' ? wave : wave + 1; open.delete(d.agent); }
    if (d.action === 'enter') {
      const iv = { from: wave + 1, to: Infinity, state: d.state, label: d.label || d.state || '' };
      if (!intervals.has(d.agent)) intervals.set(d.agent, []);
      intervals.get(d.agent).push(iv);
      open.set(d.agent, iv);
    }
  }

  function activityAt(wave, agent) {
    const list = intervals.get(agent);
    if (!list) return null;
    for (let k = list.length - 1; k >= 0; k--) {
      const iv = list[k];
      if (iv.from <= wave && wave < iv.to) return { state: iv.state, label: iv.label };
      if (iv.to <= wave) break;
    }
    return null;
  }

  return {
    hasRoster: roster.size > 0,
    at(wave, agent) {
      const r = roster.get(wave)?.get(agent);
      const activity = activityAt(wave, agent);
      if (!r && !activity) return null;
      return {
        location: r?.location ?? null,
        infection: r?.status ?? null,
        traveling: !!r?.traveling,
        activity,
      };
    },
  };
}
