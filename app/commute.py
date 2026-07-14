"""목적지 대중교통 소요시간 계산.

주소 → 좌표: 카카오 로컬 API (developers.kakao.com, REST API 키)
좌표 → 대중교통 경로: ODsay API (lab.odsay.com, 무료 1,000회/일)

같은 주소는 DB에 캐시해서 수집 주기마다 API를 다시 태우지 않는다.
MOCK=1이면 샘플 값으로 동작 (키 없이 파이프라인 검증용).

주의: 신규 택지지구는 지번이 아직 없어 지오코딩이 실패할 수 있음.
실패 시 태그를 생략할 뿐 공고 자체는 정상 표시된다.
"""
import logging
import os

import requests

from app import db

log = logging.getLogger("commute")

DEST_NAME = "목적지"
DEST_X, DEST_Y = 127.0364, 37.5006  # 경도, 위도

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


def init():
    with db.conn() as c:
        c.executescript(CACHE_SCHEMA)


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


def geocode(address: str):
    """주소 → (경도, 위도). 실패 시 None."""
    key = os.environ.get("KAKAO_REST_KEY", "").strip()
    if not key:
        return None
    headers = {"Authorization": f"KakaoAK {key}"}
    for endpoint in ("address", "keyword"):
        try:
            r = requests.get(
                f"https://dapi.kakao.com/v2/local/search/{endpoint}.json",
                params={"query": address},
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            docs = r.json().get("documents", [])
            if docs:
                return float(docs[0]["x"]), float(docs[0]["y"])
        except Exception as e:  # noqa: BLE001
            log.warning("지오코딩 실패(%s, %s): %s", endpoint, address, e)
    return None


def transit(sx: float, sy: float):
    """대중교통 최단 소요시간 → (분, 환승횟수). 실패 시 None."""
    key = os.environ.get("ODSAY_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.odsay.com/v1/api/searchPubTransPathT",
            params={"SX": sx, "SY": sy, "EX": DEST_X, "EY": DEST_Y, "apiKey": key},
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
    return None


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
    result = transit(*coords) if coords else None
    if result:
        _cache_put(address, result[0], result[1], ok=True)
        return result
    _cache_put(address, None, None, ok=False)
    return None
