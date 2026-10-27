"""임대 공고 '지원할 만한가' 판단 — NAS의 Claude(개인 구독)가 분석 결과 + 공고문 텍스트를 보고 정한다.

2026-10-27 사용자: "임대알림도 하긴 해야지… 다만 공고를 클로드가 제대로 분석해서 지원할만한 곳인지는
파악 필요. 59m2 이상만 취급하자고." → 임대 새 공고 알림은 수집 즉시 보내지 않고, 분석(cost.enrich) 뒤
이 판단을 받아 '추천 · 검토'만 보낸다. 전용 59㎡ 이상 세대가 없으면(has_59=false) 알림 · 아침 리마인더에서 뺀다.
결과는 meta `rent_review:<id>`에 저장(대시보드 · 아침 알림이 읽음).
"""
import html
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from app import cost, db, notify, rules

log = logging.getLogger("rent_review")
KST = timezone(timedelta(hours=9))
_LOCK = threading.Lock()
_KEY = "rent_review:"
_BADGE = {"추천": "✅", "검토": "🤔", "비추천": "👎", "제외": "⛔"}


def is_rent(n: dict) -> bool:
    src, kind = n.get("source"), n.get("house_kind") or ""
    return src in ("lh_rent", "gh", "applyhome_rent") or (src == "board" and "임대" in kind)


def get(nid: str):
    v = db.get_meta(_KEY + nid)
    return json.loads(v) if v else None   # 빈 값 = 다시 분석하면서 지운 것


def should_alert(r) -> bool:
    return bool(r) and r.get("has_59") is not False and r.get("verdict") in ("추천", "검토")


def badge(r) -> str:
    return _BADGE.get((r or {}).get("verdict"), "⏳")


def _prompt(n: dict, analysis, pdf: str, cfg: dict) -> str:
    profile = cfg.get("profile") or {}
    min_area = (cfg.get("rent_review") or {}).get("min_area_m2", 59)
    keep = ("deposit_manwon", "monthly_manwon", "area_m2", "units", "apply_start", "apply_end", "move_in",
            "lease_term", "income_limit", "residency_req", "rank_advice", "note")
    a = {k: analysis.get(k) for k in keep if isinstance(analysis, dict) and analysis.get(k)}
    meta = {k: n.get(k) for k in ("name", "house_kind", "region", "address", "notice_date", "r1_start", "r1_end",
                                  "commute_min") if n.get(k)}
    return (
        "가구가 이 임대주택 공고에 지원할 만한지 판단하라. JSON 객체 하나만 출력.\n"
        f"우리 상황: {cost._who(profile)}\n"
        f"희망 조건: {profile.get('wish') or ''} (출퇴근: 통근 목적지, 지하철 60분 넘으면 제외)\n"
        "★ 우리는 임대주택도 갈 수 있다(2026-10-27) — '임대라서'라는 이유만으로 제외 · 비추천하지 말 것. "
        "분양전환 여부 · 재당첨 제한 · 청약통장 사용 처리 같은 임대 특유의 손익은 reason에 적되, verdict는 아래 기준으로 정한다.\n"
        "판단 순서:\n"
        f"① 전용 {min_area}㎡ 이상인 공급 타입(주택형)이 있는가 — 공고문 공급표 · 임대조건표에서 확인. "
        f"하나도 없으면 has_59=false, verdict='제외'. 공고문으로 확인이 안 되면 has_59=null\n"
        "② 우리(부부 합산 또는 각자)가 자격 · 소득 · 자산 · 거주지 순위로 실제 신청 가능한가(가능한 사람과 순위)\n"
        "③ 보증금 · 월임대료 · 거주 가능 기간이 주변 시세 대비 메리트가 있는가, 목적지 통근 시간은 괜찮은가\n"
        "verdict: '추천'(59㎡↑ 있고 신청 가능하고 조건 좋음) · '검토'(가능하지만 따져볼 점 있음, 또는 면적 확인 불가) · "
        "'비추천'(가능하지만 메리트 적음) · '제외'(59㎡↑ 없음 또는 신청 불가)\n"
        '출력 형식: {"max_area_m2": 숫자 또는 null, "area_59_types": "59㎡ 이상 타입 요약(세대수 · 보증금/월세), 없으면 빈 문자열", '
        '"has_59": true/false/null, "verdict": "...", "reason": "한국어 1~2문장, 핵심 숫자 포함", '
        '"who": "누가 몇 순위로 가능한지 한 줄"}\n\n'
        f"공고: {json.dumps(meta, ensure_ascii=False)}\n"
        f"기존 분석(표 파싱 · 다른 모델 추출): {json.dumps(a, ensure_ascii=False)}\n"
        f"공고문 텍스트(일부):\n{(pdf or '(없음)')[:24000]}"
    )


def review(n: dict, cfg: dict):
    """1건 판단 → meta 저장 후 반환. Claude가 없거나 실패하면 None(다음 기회에 재시도)."""
    if not cost.claude_available():
        return None
    analysis = cost._cache_get(n["id"])
    try:
        text = cost._claude_generate(_prompt(n, analysis if analysis != "miss" else None,
                                             db.get_pdf_text(n["id"]) or "", cfg), timeout=300)
        m = re.search(r"\{.*\}", text, re.S)
        r = json.loads(m.group(0)) if m else None
    except Exception as e:  # noqa: BLE001
        log.warning("임대 판단 실패(%s): %s", n.get("name"), e)
        return None
    if not isinstance(r, dict) or r.get("verdict") not in _BADGE:
        return None
    r["at"] = db.now_kst()
    db.set_meta(_KEY + n["id"], json.dumps(r, ensure_ascii=False))
    log.info("임대 판단: %s → %s (59㎡↑ %s)", n.get("name"), r["verdict"], r.get("has_59"))
    return r


def message(n: dict, r: dict) -> str:
    area = r.get("area_59_types") or (f"최대 전용 {r['max_area_m2']}㎡" if r.get("max_area_m2") else "면적 공고문 확인 필요")
    lines = [f"🏠 <b>임대 새 공고 · {badge(r)} {r['verdict']}</b>", f"<b>{html.escape(n['name'], quote=False)}</b>",
             html.escape(" · ".join(x for x in (n.get("region"), n.get("house_kind")) if x), quote=False),
             "📐 " + html.escape(area, quote=False), html.escape(r.get("reason") or "", quote=False)]
    if r.get("who"):
        lines.append("👥 " + html.escape(r["who"], quote=False))
    if n.get("r1_start"):
        lines.append(f"접수 {str(n['r1_start'])[5:].replace('-', '.')}~{str(n.get('r1_end') or '')[5:].replace('-', '.')}")
    lines.append(rules._notice_url(n))
    return "\n".join(x for x in lines if x)


def _upcoming(n: dict, today) -> bool:
    ds = [n.get(k) for k in ("r1_start", "r1_end", "sp_end", "winner_date") if n.get(k)]
    return bool(ds) and max(ds)[:10] >= today.isoformat()


def backfill(ids=None):
    """아직 판단 안 한 임대 공고(접수가 끝나지 않은 것)를 하나씩 판단하고, 보류 중인 새 공고 알림을 보낸다.
    분석(cost.enrich)이 아직이면 6시간까지는 기다린다(그 뒤엔 목록 정보만으로 판단)."""
    if not _LOCK.acquire(blocking=False):
        return
    try:
        cfg = rules.load_config()
        now = datetime.now(KST)
        for n in db.list_notices(matched_only=True):
            if (ids and n["id"] not in ids) or n.get("muted") or not is_rent(n) or not _upcoming(n, now.date()):
                continue
            r = get(n["id"])
            if r is None:
                fresh = (n.get("first_seen") or "") > (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M")
                if cost._cache_get(n["id"]) == "miss" and fresh:
                    continue
                r = review(n, cfg)
            key = f"{n['id']}:new"
            if r and not db.already_sent(key) and should_alert(r):
                if notify.send_telegram(message(n, r)):
                    db.mark_sent(key)
    finally:
        _LOCK.release()
