"""SQLite storage. 공고 원본 + 알림 이력(중복 발송 방지)."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
DB_PATH = os.environ.get("DB_PATH", "/data/radar.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id            TEXT PRIMARY KEY,      -- source:notice_no
    source        TEXT NOT NULL,         -- applyhome | applyhome_remndr | lh
    name          TEXT NOT NULL,
    house_kind    TEXT,                  -- 민영/국민/신혼희망 등
    region        TEXT,                  -- 공급지역명 (서울/경기/...)
    address       TEXT,
    total_supply  INTEGER,
    notice_date   TEXT,                  -- 모집공고일 YYYY-MM-DD
    sp_start      TEXT, sp_end TEXT,     -- 특별공급 접수
    r1_start      TEXT, r1_end TEXT,     -- 1순위 접수
    r2_start      TEXT, r2_end TEXT,     -- 2순위 접수
    winner_date   TEXT,                  -- 당첨자 발표
    url           TEXT,
    flags         TEXT,                  -- JSON: 투기과열/분상제/공공택지 등 원본 플래그
    tags          TEXT,                  -- JSON: 우리 규칙으로 붙인 태그 목록
    matched       INTEGER DEFAULT 0,     -- 필터 통과 여부
    commute_min   INTEGER,               -- 목적지 대중교통 소요분(지오코딩 실패 시 NULL)
    first_seen    TEXT NOT NULL,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS sent_alerts (
    key     TEXT PRIMARY KEY,            -- notice_id:kind (new / d7 / d1 ...)
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


@contextmanager
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        try:  # 기존 DB 마이그레이션(통근시간 컬럼 추가)
            c.execute("ALTER TABLE notices ADD COLUMN commute_min INTEGER")
        except sqlite3.OperationalError:
            pass  # 이미 있음


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def upsert_notice(n: dict) -> bool:
    """공고 저장. 신규면 True (알림 트리거용)."""
    with conn() as c:
        cur = c.execute("SELECT 1 FROM notices WHERE id = ?", (n["id"],))
        is_new = cur.fetchone() is None
        c.execute(
            """INSERT INTO notices (id, source, name, house_kind, region, address,
                 total_supply, notice_date, sp_start, sp_end, r1_start, r1_end,
                 r2_start, r2_end, winner_date, url, flags, tags, matched,
                 commute_min, first_seen, raw)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, house_kind=excluded.house_kind,
                 region=excluded.region, address=excluded.address,
                 total_supply=excluded.total_supply, notice_date=excluded.notice_date,
                 sp_start=excluded.sp_start, sp_end=excluded.sp_end,
                 r1_start=excluded.r1_start, r1_end=excluded.r1_end,
                 r2_start=excluded.r2_start, r2_end=excluded.r2_end,
                 winner_date=excluded.winner_date, url=excluded.url,
                 flags=excluded.flags, tags=excluded.tags, matched=excluded.matched,
                 commute_min=excluded.commute_min, raw=excluded.raw""",
            (
                n["id"], n["source"], n["name"], n.get("house_kind"), n.get("region"),
                n.get("address"), n.get("total_supply"), n.get("notice_date"),
                n.get("sp_start"), n.get("sp_end"), n.get("r1_start"), n.get("r1_end"),
                n.get("r2_start"), n.get("r2_end"), n.get("winner_date"), n.get("url"),
                json.dumps(n.get("flags", {}), ensure_ascii=False),
                json.dumps(n.get("tags", []), ensure_ascii=False),
                1 if n.get("matched") else 0,
                n.get("commute_min"),
                now_kst(),
                json.dumps(n.get("raw", {}), ensure_ascii=False),
            ),
        )
        return is_new


def list_notices(matched_only: bool = True):
    q = "SELECT * FROM notices"
    if matched_only:
        q += " WHERE matched = 1"
    q += " ORDER BY COALESCE(r1_start, sp_start, notice_date) DESC"
    llm, hist = {}, {}
    with conn() as c:
        rows = [dict(r) for r in c.execute(q).fetchall()]
        try:  # LLM 분석결과·히스토리(cost_cache) 병합
            for cr in c.execute("SELECT notice_id, result, ok, history FROM cost_cache").fetchall():
                llm[cr["notice_id"]] = json.loads(cr["result"]) if (cr["ok"] and cr["result"]) else None
                hist[cr["notice_id"]] = json.loads(cr["history"]) if cr["history"] else []
        except sqlite3.OperationalError:
            pass
    for r in rows:
        r["flags"] = json.loads(r["flags"] or "{}")
        r["tags"] = json.loads(r["tags"] or "[]")
        r.pop("raw", None)
        r["enriched"] = r["id"] in llm           # LLM 분석 실행 여부
        r["llm"] = llm.get(r["id"])              # 결과 dict / None(실패) / 없음
        r["llm_history"] = hist.get(r["id"], []) # 최근 3개 결과
    return rows


def deactivate_board_noise(keywords: list) -> int:
    """이름에 후속글 키워드(발표·경쟁률 등)가 든 게시판 공고를 DB에서 직접 비활성화.
    크롤 피드에서 밀려나 재수집·재평가되지 않는 기존 행까지 확실히 정리한다."""
    if not keywords:
        return 0
    where = " OR ".join(["name LIKE ?"] * len(keywords))
    with conn() as c:
        cur = c.execute(
            f"UPDATE notices SET matched = 0 WHERE source = 'board' AND matched = 1 AND ({where})",
            [f"%{k}%" for k in keywords],
        )
        return cur.rowcount


def deactivate_stale_boards(days: int = 45) -> int:
    """접수 마감일이 불확실한 게시판(board) 공고 중 공고일이 오래된 것을 비활성화한다.
    크롤 첫 페이지에서 밀려나 재수집되지 않는 옛 게시글이 matched=1로 남는 것을 정리.
    (board는 접수 종료일이 없어 날짜 기반 마감 판정이 안 되므로 공고일 경과로 처리)"""
    cutoff = (datetime.now(KST) - timedelta(days=days)).date().isoformat()
    with conn() as c:
        cur = c.execute(
            "UPDATE notices SET matched = 0 "
            "WHERE source = 'board' AND matched = 1 "
            "AND notice_date IS NOT NULL AND notice_date < ?",
            (cutoff,),
        )
        return cur.rowcount


def get_notice(nid: str):
    """공고 단건(raw 포함) — 온디맨드 LLM 분석용."""
    with conn() as c:
        row = c.execute("SELECT * FROM notices WHERE id = ?", (nid,)).fetchone()
    if not row:
        return None
    n = dict(row)
    n["flags"] = json.loads(n["flags"] or "{}")
    n["tags"] = json.loads(n["tags"] or "[]")
    n["raw"] = json.loads(n["raw"] or "{}")
    return n


def already_sent(key: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM sent_alerts WHERE key = ?", (key,)).fetchone() is not None


def mark_sent(key: str):
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO sent_alerts (key, sent_at) VALUES (?, ?)",
            (key, now_kst()),
        )


def set_meta(k: str, v: str):
    with conn() as c:
        c.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )


def get_meta(k: str):
    with conn() as c:
        row = c.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row["v"] if row else None
