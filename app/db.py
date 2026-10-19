"""SQLite storage. 공고 원본 + 알림 이력(중복 발송 방지)."""
import json
import os
import sqlite3
import time
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
CREATE TABLE IF NOT EXISTS muted_notices (
    notice_id TEXT PRIMARY KEY,          -- 알림 끈 공고(관심 없음) → 대시보드 접힘 + 발송 제외
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memos (
    notice_id TEXT PRIMARY KEY,          -- 공고별 사용자 메모(대시보드 타이틀 아래 상시 표시)
    text      TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notice_qa (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id TEXT NOT NULL,             -- 분석 후 추가질문 기록
    q         TEXT NOT NULL,
    a         TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pdf_texts (
    notice_id TEXT PRIMARY KEY,          -- LLM 분석 때 뽑은 공고문 텍스트 캐시(추가질문 컨텍스트)
    text      TEXT NOT NULL,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collapsed_notices (
    notice_id TEXT PRIMARY KEY,          -- 접기(알림은 계속 발송, 대시보드만 1줄 표시)
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token  TEXT PRIMARY KEY,             -- 대시보드 로그인 세션(재배포에도 유지되도록 DB에 저장)
    expiry REAL NOT NULL                 -- 만료 epoch(초)
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
    llm, hist, muted = {}, {}, set()
    memos, qa = {}, {}
    collapsed = set()
    with conn() as c:
        rows = [dict(r) for r in c.execute(q).fetchall()]
        try:  # LLM 분석결과·히스토리(cost_cache) 병합
            for cr in c.execute("SELECT notice_id, result, ok, history FROM cost_cache").fetchall():
                llm[cr["notice_id"]] = json.loads(cr["result"]) if (cr["ok"] and cr["result"]) else None
                hist[cr["notice_id"]] = json.loads(cr["history"]) if cr["history"] else []
        except sqlite3.OperationalError:
            pass
        try:  # 알림 끈 공고
            muted = {x["notice_id"] for x in c.execute("SELECT notice_id FROM muted_notices").fetchall()}
        except sqlite3.OperationalError:
            pass
        try:  # 접은 공고(알림은 계속 발송)
            collapsed = {x["notice_id"] for x in c.execute("SELECT notice_id FROM collapsed_notices").fetchall()}
        except sqlite3.OperationalError:
            pass
        try:  # 공고별 메모
            memos = {x["notice_id"]: x["text"] for x in c.execute("SELECT notice_id, text FROM memos")}
        except sqlite3.OperationalError:
            pass
        try:  # 추가질문 기록(공고별 시간순)
            for x in c.execute("SELECT notice_id, q, a, at FROM notice_qa ORDER BY id"):
                qa.setdefault(x["notice_id"], []).append({"q": x["q"], "a": x["a"], "at": x["at"]})
        except sqlite3.OperationalError:
            pass
    for r in rows:
        r["flags"] = json.loads(r["flags"] or "{}")
        r["tags"] = json.loads(r["tags"] or "[]")
        r.pop("raw", None)
        r["enriched"] = r["id"] in llm           # LLM 분석 실행 여부
        r["llm"] = llm.get(r["id"])              # 결과 dict / None(실패) / 없음
        r["llm_history"] = hist.get(r["id"], []) # 최근 3개 결과
        r["muted"] = r["id"] in muted            # 알림 끔(관심 없음)
        r["collapsed"] = r["id"] in collapsed    # 접기(알림은 계속 발송)
        r["memo"] = memos.get(r["id"], "")       # 사용자 메모
        r["qa"] = qa.get(r["id"], [])            # 추가질문 기록
    return rows


def deactivate_board_noise(keywords: list) -> int:
    """이름에 후속글 키워드(발표·경쟁률 등)가 든 게시판 공고를 DB에서 직접 비활성화.
    크롤 피드에서 밀려나 재수집·재평가되지 않는 기존 행까지 확실히 정리한다."""
    if not keywords:
        return 0
    # 띄어쓰기 무시 비교 — rules.process의 노이즈 판정과 같은 기준
    where = " OR ".join(["REPLACE(name, ' ', '') LIKE ?"] * len(keywords))
    with conn() as c:
        cur = c.execute(
            f"UPDATE notices SET matched = 0 WHERE source = 'board' AND matched = 1 AND ({where})",
            [f"%{k.replace(' ', '')}%" for k in keywords],
        )
        return cur.rowcount


def deactivate_rent_types(allowed: list) -> int:
    """LH·GH 임대 중 허용 유형(config lh_gh_rent_types)이 아닌 공고를 비활성화.
    유형을 config에서 뺐을 때, 재수집되지 않아 DB에 남는 기존 행까지 정리한다."""
    if not allowed:
        return 0
    keep = " OR ".join(["house_kind LIKE ?"] * len(allowed))
    with conn() as c:
        cur = c.execute(
            f"UPDATE notices SET matched = 0 WHERE source IN ('lh_rent','gh') "
            f"AND matched = 1 AND NOT ({keep})",
            [f"%{k}%" for k in allowed],
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


def set_muted(nid: str, muted: bool):
    """알림 끄기/켜기. 끄면 신규·리마인더 알림 발송에서 제외되고 대시보드가 접는다."""
    with conn() as c:
        if muted:
            c.execute("INSERT OR IGNORE INTO muted_notices (notice_id, at) VALUES (?, ?)", (nid, now_kst()))
        else:
            c.execute("DELETE FROM muted_notices WHERE notice_id = ?", (nid,))


def set_collapsed(nid: str, collapsed: bool):
    """접기/펼치기. 알림 발송에는 영향 없고 대시보드만 1줄로 표시."""
    with conn() as c:
        if collapsed:
            c.execute("INSERT OR IGNORE INTO collapsed_notices (notice_id, at) VALUES (?, ?)", (nid, now_kst()))
        else:
            c.execute("DELETE FROM collapsed_notices WHERE notice_id = ?", (nid,))


def set_memo(nid: str, text: str):
    """공고 메모 저장. 빈 문자열이면 삭제."""
    text = (text or "").strip()
    with conn() as c:
        if text:
            c.execute(
                "INSERT INTO memos (notice_id, text, at) VALUES (?,?,?) "
                "ON CONFLICT(notice_id) DO UPDATE SET text=excluded.text, at=excluded.at",
                (nid, text, now_kst()),
            )
        else:
            c.execute("DELETE FROM memos WHERE notice_id = ?", (nid,))


def add_qa(nid: str, q: str, a: str):
    """추가질문 1건 기록."""
    with conn() as c:
        c.execute("INSERT INTO notice_qa (notice_id, q, a, at) VALUES (?,?,?,?)",
                  (nid, q, a, now_kst()))


def list_qa(nid: str):
    """공고의 추가질문 기록(시간순)."""
    with conn() as c:
        return [{"q": r["q"], "a": r["a"], "at": r["at"]}
                for r in c.execute("SELECT q, a, at FROM notice_qa WHERE notice_id = ? ORDER BY id", (nid,))]


def set_pdf_text(nid: str, text: str):
    """공고문 텍스트 캐시(추가질문 컨텍스트용)."""
    with conn() as c:
        c.execute(
            "INSERT INTO pdf_texts (notice_id, text, at) VALUES (?,?,?) "
            "ON CONFLICT(notice_id) DO UPDATE SET text=excluded.text, at=excluded.at",
            (nid, text, now_kst()),
        )


def get_pdf_text(nid: str):
    with conn() as c:
        row = c.execute("SELECT text FROM pdf_texts WHERE notice_id = ?", (nid,)).fetchone()
        return row["text"] if row else None


def is_muted(nid: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM muted_notices WHERE notice_id = ?", (nid,)).fetchone() is not None


# ── 로그인 세션(재배포에도 유지) ────────────────────────────────
def session_add(token: str, expiry: float):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO sessions (token, expiry) VALUES (?, ?)", (token, expiry))


def session_valid(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE expiry < ?", (now,))  # 만료 정리
        row = c.execute("SELECT expiry FROM sessions WHERE token = ?", (token,)).fetchone()
    return bool(row and row["expiry"] > now)


def session_del(token: str):
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))
