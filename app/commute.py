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
import re

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
