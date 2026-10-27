"""목적지 대중교통 소요시간 계산.

주소 → 좌표: 카카오 로컬 API (developers.kakao.com, REST API 키)
좌표 → 대중교통 경로: 네이버 지도 대중교통(지하철+도보 경로만, 평일 08시 출발) 우선, 실패 시 ODsay 지하철 전용
  (2026-10-27: ODsay 기본값은 버스 섞인 경로라 구리갈매가 51분으로 나왔는데 지하철로는 71~74분 →
   사용자 기준 '지하철 1시간, 넘으면 제외'에 맞게 바꿈. 사용자가 직접 확인하는 것도 네이버)

같은 주소는 DB에 캐시해서 수집 주기마다 API를 다시 태우지 않는다.
MOCK=1이면 샘플 값으로 동작 (키 없이 파이프라인 검증용).

주의: 신규 택지지구는 지번이 아직 없어 지오코딩이 실패할 수 있음.
실패 시 태그를 생략할 뿐 공고 자체는 정상 표시된다.
"""
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from app import db

log = logging.getLogger("commute")

# 실제 출근지 통근 목적지 — 2026-10-27 목적지 좌표에서 변경
DEST_NAME = "통근 목적지"
DEST_X, DEST_Y = 127.0329295, 37.5000693  # 경도, 위도

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS commute_cache (
    address    TEXT PRIMARY KEY,
    minutes    INTEGER,
    transfers  INTEGER,
    ok         INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);
"""

MOCK_TIMES = {  # MOCK 모드용 대략치
    "부천시": (66, 1),
    "하남시": (74, 1),
    "성동구": (27, 1),
}


_SRC = "naver_subway_mobileleader_v2"   # 계산 방식이 바뀌면 캐시를 비운다
NO_SUBWAY = 999   # 지하철+도보만으로 가는 경로가 없음(버스 필요) — 60분 초과처럼 제외된다
# 네이버에 몰아서 조회하지 않게(2026-10-27: 160건 몰아 부른 뒤 짧은 UA가 400으로 거부됨 — 실제 크롬 UA로 해결).
# 실제 경로 조회는 15분에 20건까지만 하고, 나머지는 다음 수집 때로 미룬다(실패도 캐시하지 않음).
_LIVE: list = []
_MAX_LIVE, _WINDOW = 20, 900


def _budget_ok() -> bool:
    now = time.time()
    _LIVE[:] = [t for t in _LIVE if now - t < _WINDOW]
    if len(_LIVE) >= _MAX_LIVE:
        return False
    _LIVE.append(now)
    return True


def init():
    with db.conn() as c:
        c.executescript(CACHE_SCHEMA)
    if db.get_meta("commute_src") != _SRC:
        with db.conn() as c:
            c.execute("DELETE FROM commute_cache")
        db.set_meta("commute_src", _SRC)
        log.info("통근 캐시 초기화(계산 방식 %s)", _SRC)


def _cache_get(address: str):
    with db.conn() as c:
        row = c.execute("SELECT * FROM commute_cache WHERE address = ?", (address,)).fetchone()
        return dict(row) if row else None


def _cache_put(address: str, minutes, transfers, ok: bool):
    with db.conn() as c:
        c.execute(
            """INSERT INTO commute_cache (address, minutes, transfers, ok, checked_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(address) DO UPDATE SET minutes=excluded.minutes,
                 transfers=excluded.transfers, ok=excluded.ok, checked_at=excluded.checked_at""",
            (address, minutes, transfers, 1 if ok else 0, db.now_kst()),
        )


def _geocode_once(query: str, headers: dict):
    for endpoint in ("address", "keyword"):
        try:
            r = requests.get(
                f"https://dapi.kakao.com/v2/local/search/{endpoint}.json",
                params={"query": query},
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            docs = r.json().get("documents", [])
            if docs:
                return float(docs[0]["x"]), float(docs[0]["y"])
        except Exception as e:  # noqa: BLE001
            log.warning("지오코딩 실패(%s, %s): %s", endpoint, query, e)
    return None


def _geocode_candidates(address: str):
    """지오코딩 질의 후보를 순서대로 생성.
    무순위 등은 정식 주소 대신 단지명('평택 고덕 우미 린 프레스티지(Abc-36BL)')만 있어
    전체로는 실패한다. 괄호·특수문자를 떼고, 그래도 안 되면 뒤 단어부터 하나씩 잘라 재시도한다."""
    seen = []

    def add(q):
        q = q.strip(" ·-,")
        if q and q not in seen:
            seen.append(q)

    add(address)
    cleaned = re.sub(r"[(（\[].*", "", address)          # 괄호 이후 제거
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    add(cleaned)
    words = cleaned.split(" ")
    for i in range(len(words) - 1, 1, -1):               # 뒤에서부터 한 단어씩 제거(최소 2단어까지)
        add(" ".join(words[:i]))
    return seen


def geocode(address: str):
    """주소 → (경도, 위도). 실패 시 뒤 단어부터 잘라가며 재시도. 모두 실패면 None."""
    key = os.environ.get("KAKAO_REST_KEY", "").strip()
    if not key:
        return None
    headers = {"Authorization": f"KakaoAK {key}"}
    for query in _geocode_candidates(address):
        coords = _geocode_once(query, headers)
        if coords:
            if query != address:
                log.info("지오코딩 폴백 성공: %r → %r", address, query)
            return coords
    return None


_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


def _next_weekday_8am() -> str:
    d = datetime.now(timezone(timedelta(hours=9))).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return f"{d.isoformat()}T08:00:00"


def naver(sx: float, sy: float):
    """네이버 지도 대중교통 — 지하철+도보만인 경로 중 최단 (분, 환승횟수).
    경로는 있는데 전부 버스가 섞였으면 "nosub", 조회 실패 · 경로 없음이면 None."""
    try:
        r = requests.get(
            "https://map.naver.com/p/api/directions/pubtrans",
            params={"start": f"{sx},{sy}", "goal": f"{DEST_X},{DEST_Y}", "crs": "EPSG:4326",
                    "mode": "TIME", "lang": "ko", "departureTime": _next_weekday_8am()},
            # 짧은 UA('Mozilla/5.0 (Macintosh) Chrome/140')는 2026-10-27부터 400으로 거부됨 → 실제 크롬 UA
            headers={"User-Agent": _UA, "Referer": "https://map.naver.com/"},
            timeout=20,
        )
        r.raise_for_status()
        best, paths = None, r.json().get("paths") or []
        for p in paths:
            steps = [st for leg in p.get("legs") or [] for st in leg.get("steps") or []]
            if not steps or any(st.get("type") not in ("WALKING", "SUBWAY") for st in steps):
                continue
            n_sub = sum(1 for st in steps if st.get("type") == "SUBWAY")
            if p.get("duration") is not None and (best is None or p["duration"] < best[0]):
                best = (int(p["duration"]), max(n_sub - 1, 0))
        return best or ("nosub" if paths else None)
    except Exception as e:  # noqa: BLE001
        log.warning("네이버 경로 조회 실패: %s", e)
    return None


def transit(sx: float, sy: float):
    """지하철 최단 소요시간 → (분, 환승횟수). 네이버 우선, 없으면 ODsay 지하철 전용. 실패 시 None."""
    nv = naver(sx, sy)   # (분, 환승) · "nosub"(버스 포함 경로만) · None(조회 실패)
    time.sleep(2.5)   # 네이버에 몰아서 요청하지 않게
    if isinstance(nv, tuple):
        return nv
    key = os.environ.get("ODSAY_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.odsay.com/v1/api/searchPubTransPathT",
            params={"SX": sx, "SY": sy, "EX": DEST_X, "EY": DEST_Y, "SearchPathType": 1, "apiKey": key},
            timeout=20,
        )
        r.raise_for_status()
        paths = (r.json().get("result") or {}).get("path") or []
        best = None
        for p in paths[:5]:
            info = p.get("info") or {}
            t = info.get("totalTime")
            if t is not None and (best is None or t < best[0]):
                best = (int(t), int(info.get("busTransitCount", 0)) + int(info.get("subwayTransitCount", 0)) - 1)
        if best:
            return best[0], max(best[1], 0)
    except Exception as e:  # noqa: BLE001
        log.warning("ODsay 경로 조회 실패: %s", e)
    return (NO_SUBWAY, 0) if nv == "nosub" else None


def to_yeoksam(address: str):
    """주소 → (분, 환승횟수) 또는 None. 캐시 우선."""
    if not address:
        return None

    if os.environ.get("MOCK") == "1":
        for k, v in MOCK_TIMES.items():
            if k in address:
                return v
        return None

    cached = _cache_get(address)
    if cached:
        return (cached["minutes"], cached["transfers"]) if cached["ok"] else None

    coords = geocode(address)
    if not coords:   # 주소 자체를 못 찾음 — 캐시해 두고 다시 묻지 않는다
        _cache_put(address, None, None, ok=False)
        return None
    if not _budget_ok():   # 이번 15분 몫을 다 씀 → 판단 보류(캐시 안 함, 다음 수집 때 계산)
        return None
    result = transit(*coords)
    if result:
        _cache_put(address, result[0], result[1], ok=True)
    return result   # 조회 실패(네이버 차단 · ODsay 한도)는 캐시하지 않아 다음에 다시 시도
