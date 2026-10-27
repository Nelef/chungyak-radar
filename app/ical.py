"""구글 캘린더 구독용 iCalendar(.ics) — 분양 공고의 접수·발표 일정만, 중복은 자동 정리.

GET /cal/<CAL_TOKEN>.ics (main.py). 구글 캘린더 '다른 캘린더 → URL로 추가'로 구독하면
별도 캘린더로 보여 켜고 끌 수 있다(구글이 몇 시간마다 다시 읽음).

정리 규칙:
- 필터 통과(matched) · 알림 안 끈 공고만, 임대(lh_rent·gh·'임대' 유형)는 제외 — 희망 조건이 분양만
- config `calendar.skip_ids`: 메인 캘린더에 직접 넣은 공고(중복 방지)
- 접수 기간이 3일 이상이면 시작일 · 마감일 두 일정으로(긴 막대 대신)
- NAS의 Claude가 묶어 둔 결과(dedupe.py)가 있으면 그걸 먼저 쓴다(대표 공고 + 짧은 제목)
- 그 밖의 공고는 이름 규칙으로: 같은 단지 공고가 여럿이면(이름에서 [정정공고]·[무순위]·(3차)·(공고일) 등을 떼고 비교)
  · 정정공고가 섞여 있으면 가장 최근 공고 하나만 남긴다
  · 일정이 똑같은 회차(1차·3차 등)는 한 일정으로 묶는다
"""
import re
from datetime import date, datetime, timedelta, timezone

from app import dedupe, rules

_RENT_SOURCES = ("lh_rent", "gh")
_LONG_DAYS = 3   # 이보다 긴 접수 기간은 시작 · 마감 하루짜리 두 일정으로(월 보기에 긴 막대가 깔리지 않게)


def _base_name(name: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", name or "")
    s = re.sub(r"\((?:\d+차|[\d.'’ ]+(?:공고)?)\)", " ", s)
    s = re.sub(r"(?:최초|추가|잔여세대)?\s*(?:예비)?입주자\s*모집\s*(?:공고)?|모집\s*공고", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _esc(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: 한 줄 75바이트 넘으면 접는다(UTF-8 글자 중간은 자르지 않음)."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def _wanted(n: dict, skip: set) -> bool:
    if n.get("muted") or n["id"] in skip:
        return False
    if n.get("source") in _RENT_SOURCES or "임대" in (n.get("house_kind") or ""):
        return False
    return any(n.get(k) for k in ("sp_start", "r1_start", "winner_date"))


def _tidy(notices: list[dict]) -> list[tuple[dict, int]]:
    """(대표 공고, 묶인 건수) 목록."""
    groups: dict[str, list[dict]] = {}
    for n in notices:
        groups.setdefault(_base_name(n["name"]), []).append(n)
    out = []
    for g in groups.values():
        g.sort(key=lambda n: (n.get("notice_date") or "", n["id"]), reverse=True)
        if any("정정" in n["name"] for n in g):
            g = g[:1]
        same: dict[tuple, list[dict]] = {}
        for n in g:
            same.setdefault((n.get("sp_start"), n.get("sp_end"), n.get("r1_start"), n.get("r1_end"),
                             n.get("winner_date")), []).append(n)
        out += [(v[0], len(v)) for v in same.values()]
    return out


def _llm_tidy(notices: list[dict]):
    """Claude 묶음 적용 → ([(대표, 건수, 제목)], 묶음에 없는 공고)."""
    g = dedupe.groups_by_id()
    by, rest = {}, []
    for n in notices:
        grp = g.get(n["id"])
        if grp:
            by.setdefault(tuple(grp["ids"]), (grp, []))[1].append(n)
        else:
            rest.append(n)
    out = []
    for grp, members in by.values():
        rep = next((m for m in members if m["id"] == grp["keep"]), None) \
            or max(members, key=lambda m: m.get("notice_date") or "")
        out.append((rep, len(members), grp.get("title") or ""))
    return out, rest


def build(notices: list[dict], cfg: dict) -> str:
    c = cfg.get("calendar") or {}
    skip = set(c.get("skip_ids") or [])
    today = datetime.now(timezone(timedelta(hours=9))).date()
    lo, hi = today - timedelta(days=c.get("past_days", 7)), today + timedelta(days=c.get("future_days", 180))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//chungyak-radar//ko", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:" + _esc(c.get("name", "청약 레이더 (자동)")),
             "X-WR-TIMEZONE:Asia/Seoul", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]
    picked, rest = _llm_tidy([n for n in notices if _wanted(n, skip)])
    for n, cnt, title in picked + [(n, c, "") for n, c in _tidy(rest)]:
        name = title or (_base_name(n["name"]) + (f" (회차 {cnt}건)" if cnt > 1 else ""))
        r1 = "1순위 접수" if n.get("source") == "applyhome" else "접수"
        desc = [f"유형: {n.get('house_kind') or '-'}"] + ([f"같은 모집 공고 {cnt}건을 하나로 정리"] if cnt > 1 else [])
        if n.get("total_supply"):
            desc.append(f"공급: {n['total_supply']}세대")
        if n.get("commute_min"):
            desc.append(f"통근 목적지 지하철 약 {n['commute_min']}분")
        if n.get("url"):
            desc.append(f"공고: {n['url']}")
        desc.append(f"레이더: {rules._notice_url(n)}")
        for kind, start, end, label in (("sp", n.get("sp_start"), n.get("sp_end"), "🏢 특공 접수"),
                                        ("r1", n.get("r1_start"), n.get("r1_end"), f"🏢 {r1}"),
                                        ("win", n.get("winner_date"), None, "📢 발표")):
            s = _d(start)
            if not s:
                continue
            e = _d(end) or s
            if e < s:
                e = s
            if (e - s).days >= _LONG_DAYS:
                what = label.split(" ", 1)[1]
                parts = [(f"{kind}-s", s, s, f"{label} 시작 · {name} (~{e.month}/{e.day})"),
                         (f"{kind}-e", e, e, f"⏰ {what} 마감 · {name}")]
            else:
                parts = [(kind, s, e, f"{label} · {name}")]
            for uid, ps, pe, title in parts:
                if pe < lo or ps > hi:
                    continue
                lines += ["BEGIN:VEVENT", f"UID:{n['id']}-{uid}@chungyak-radar", f"DTSTAMP:{stamp}",
                          f"DTSTART;VALUE=DATE:{ps:%Y%m%d}", f"DTEND;VALUE=DATE:{pe + timedelta(days=1):%Y%m%d}",
                          "SUMMARY:" + _esc(title), "DESCRIPTION:" + _esc("\n".join(desc)),
                          "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"
