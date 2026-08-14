// frontend/js/sim/settings/server-list.js
// GET /api/servers 결과를 공유 캐시로 들고 있는 모듈.
//
// 설정 패널에는 서버 드롭다운이 여러 개 있다 — 시뮬레이션 레벨 1개 +
// 에이전트 카드마다 1개. 카드 렌더링 때마다 fetch 하면 에이전트 수만큼
// 요청이 나가므로, 여기서 한 번만 가져와 모두가 공유한다.
//
// 무효화 시점:
//   - 설정 패널을 열 때(renderSettingsPage)
//   - 서버 모달에서 목록을 새로 읽을 때(servers.js loadServers — 모달 오픈/추가/수정/삭제 후)

let _cache    = null;  // Server[] | null
let _inFlight = null;  // Promise<Server[]> | null
let _epoch    = 0;     // invalidate 이후 도착하는 stale 응답을 구분하기 위한 세대 카운터

/** 캐시를 버린다. 다음 getServerList() 호출에서 다시 fetch 한다. */
export function invalidateServerList() {
  _cache    = null;
  _inFlight = null;
  _epoch++;
}

/** 캐시된 목록을 동기적으로 본다. 없으면 null (렌더링 깜빡임 방지용). */
export function peekServerList() {
  return _cache;
}

/**
 * 서버 목록을 반환한다. 캐시가 있으면 그대로, 없으면 fetch 한다.
 * 동시 호출은 같은 요청 하나로 합쳐진다. 실패 시 throw 하지 않고 [] 를 주지만
 * 그 결과는 캐시하지 않는다(일시적 네트워크 오류로 이후 재시도가 막히는 것을 방지).
 */
export function getServerList() {
  if (_cache) return Promise.resolve(_cache);
  if (_inFlight) return _inFlight;

  const myEpoch = _epoch;
  _inFlight = fetch('/api/servers')
    .then(res => (res.ok ? res.json() : []))
    .catch(err => {
      console.error('[sim] 서버 목록 불러오기 실패:', err);
      return null; // 실패는 캐시하지 않음 — null 로 구분
    })
    .then(list => {
      // fetch 도중 invalidateServerList() 가 호출됐으면(다음 세대로 넘어갔으면)
      // 이 응답은 이미 낡은 것이니 캐시에 반영하지 않는다.
      if (myEpoch !== _epoch) return _cache || [];
      _inFlight = null;
      if (list === null) return []; // 실패 — 캐시 안 함, 다음 호출에서 재시도
      _cache = Array.isArray(list) ? list : [];
      return _cache;
    });

  return _inFlight;
}
