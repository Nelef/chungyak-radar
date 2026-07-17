"""필터 + 태깅 규칙.

config.yaml 기준으로 관심 지역을 거르고,
두 사람 상황(서울 2년 거주·각자 세대주·혼인신고 전·생애최초)에 맞는
행동 태그를 공고에 붙인다.
"""
import os
from datetime import date

import yaml

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

DEFAULT_CONFIG = {
    "poll_interval_minutes": 60,
    "mode": "premarital",  # premarital | married
    "regions": {
        "seoul": True,
        "gyeonggi_cities": ["하남", "과천", "성남", "광명", "안양", "의왕"],
    },
    "exclude_keywords": ["오피스텔", "도시형생활주택", "생활숙박"],
    "reminders": [7, 1],  # 접수 시작 D-7, D-1 리마인더
    "commute": {
        "enabled": True,
        "max_minutes": 60,      # 기준 초과 시 경고 태그
        "exclude_over": False,  # True면 기준 초과 공고를 아예 제외
    },
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except FileNotFoundError:
        user = {}
    cfg = {**DEFAULT_CONFIG, **user}
    cfg["regions"] = {**DEFAULT_CONFIG["regions"], **(user.get("regions") or {})}
    return cfg


def match_region(n: dict, cfg: dict) -> bool:
    region = (n.get("region") or "") + " " + (n.get("address") or "")
    if cfg["regions"].get("seoul") and "서울" in region:
        return True
    for city in cfg["regions"].get("gyeonggi_cities", []):
        if city in region:
            return True
    return False


def excluded(n: dict, cfg: dict) -> bool:
    name = (n.get("name") or "") + (n.get("house_kind") or "")
    return any(kw in name for kw in cfg.get("exclude_keywords", []))


def build_tags(n: dict, cfg: dict) -> list[str]:
    tags = []
    region = (n.get("region") or "") + " " + (n.get("address") or "")
    name = n.get("name") or ""
    flags = n.get("flags") or {}

    if "서울" in region:
        tags.append("서울 당해 — 2년 거주 우선 대상")
    else:
        if flags.get("대규모택지"):
            tags.append("경기 대규모택지 — 서울 거주자는 수도권 50% 풀")
        else:
            tags.append("경기 — 당해 우선 확인 필요")

    if flags.get("투기과열") or flags.get("조정대상"):
        tags.append("규제지역 — 당첨 시 재당첨 제한 (신중히)")
    if flags.get("분양가상한제"):
        tags.append("분상제 — 시세 대비 저렴, 전매·실거주 의무 확인")

    if n.get("source") == "applyhome_remndr" or name.startswith("[무순위]"):
        # 무순위(줍줍)는 청약통장을 안 쓰므로 당첨·포기해도 통장 유지 → 통장 리스크 0.
        # 다만 규제지역 주택이면 당첨 시 재당첨 제한이 걸릴 수 있어 공고문 확인이 필요.
        # 100% 추첨이라 특공/순위·혼인신고 태그는 붙이지 않고 여기서 반환한다.
        tags.append("무순위(줍줍) — 청약통장 미사용(당첨·포기해도 통장 유지). "
                    "단 규제지역 주택이면 당첨 시 재당첨 제한 가능 → 공고문 확인")
        return tags

    is_hopetown = "신혼희망" in name
    if is_hopetown:
        tags.append("⚠ 신혼희망타운 — 수익공유형 모기지 의무 여부 공고문 확인")

    if cfg.get("mode") == "premarital":
        if is_hopetown:
            tags.append("혼인신고 전 — 예비신혼부부로 신청(공고일부터 1년 내 혼인 증명, 둘이 한 장)")
        else:
            tags.append("혼인신고 전 — 각자 신청 검토(민영 생초 1인가구 60㎡↓ / 공공 청년)")
    else:
        tags.append("기혼 — 신혼(7년 내)·생초 특공 전 물량 대상")
    return tags


def process(notices: list[dict]) -> list[dict]:
    """수집 원본에 matched/tags를 채워서 반환."""
    from app import commute  # 순환 임포트 방지

    cfg = load_config()
    ccfg = {**DEFAULT_CONFIG["commute"], **(cfg.get("commute") or {})}
    # LLM 분석(필요현금·순위·임대료)은 수집 때 자동 실행하지 않고, 대시보드에서
    # 공고별 '분석' 버튼을 눌렀을 때 POST /api/enrich 로 온디맨드 실행한다(cost.enrich).
    for n in notices:
        if n.get("source") == "board":  # SH·청년안심 임대 공고
            # 모집공고가 아닌 후속 게시글(발표·경쟁률·사전점검 등)은 접수일정이 없어
            # '접수 확인'으로 잘못 떠 대시보드를 오염시키므로 제외한다.
            name = n.get("name") or ""
            noise = any(kw in name for kw in cfg.get("board_exclude_keywords", []))
            n["matched"] = not excluded(n, cfg) and not noise
            n["tags"] = build_rental_tags(n, cfg) if n["matched"] else []
            continue  # 주소가 불확실해 통근 계산은 생략 (신청기간은 buttons로 분석)
        if n.get("source") in ("lh_rent", "gh"):  # LH임대·GH — 지역 대신 통근 1시간 이내만 남김
            n["tags"] = build_rental_tags(n, cfg)
            n["matched"] = not excluded(n, cfg)
            if n["matched"] and ccfg.get("enabled"):
                result = commute.to_yeoksam(n.get("address") or n.get("region") or "")
                if result:
                    minutes, transfers = result
                    n["commute_min"] = minutes
                    if minutes > ccfg["max_minutes"]:
                        n["matched"] = False  # 목적지 1시간 초과 제외(사용자 요청)
                    else:
                        n["tags"].insert(0, f"🚇 목적지 약 {minutes}분 · 환승 {transfers}회")
                else:  # 지오코딩 실패 — 거르지 않고 남기되 통근 미확인임을 표시
                    n["tags"].insert(0, "🚇 통근시간 확인 필요(주소 미확인)")
            continue
        n["matched"] = match_region(n, cfg) and not excluded(n, cfg)
        if n.get("source") == "applyhome_rent":  # 공공지원 민간임대 — 지역/통근 필터 + 임대 태그
            n["tags"] = build_rental_tags(n, cfg) if n["matched"] else []
        else:
            n["tags"] = build_tags(n, cfg) if n["matched"] else []
        if not n["matched"]:
            continue
        # 매칭된 공고만 통근시간 계산 (주소별 캐시라 API 사용량 최소)
        if ccfg.get("enabled"):
            result = commute.to_yeoksam(n.get("address") or n.get("region") or "")
            if result:
                minutes, transfers = result
                n["commute_min"] = minutes
                over = minutes > ccfg["max_minutes"]
                mark = "⚠ " if over else ""
                suffix = f" — {ccfg['max_minutes']}분 초과" if over else ""
                n["tags"].insert(0, f"{mark}🚇 목적지 약 {minutes}분 · 환승 {transfers}회{suffix}")
                if over and ccfg.get("exclude_over"):
                    n["matched"] = False
    return notices


def build_rental_tags(n: dict, cfg: dict) -> list[str]:
    """SH·청년안심 임대 공고 태그. 임대는 무주택 유지 + 통장·재당첨 무관."""
    name = n.get("name") or ""
    tags = ["임대 — 무주택 유지, 청약통장·재당첨 제한 무관"]
    if "미리내집" in name or "장기전세" in name:
        tags.append("★ 미리내집/장기전세 — 시세 80%↓ 전세, 맞벌이 소득 완화(우선배정 가능)")
    # 청년안심주택 포털(BMSR00015) 공고는 게시판 전체가 청년안심주택이라
    # 제목에 '청년안심' 글자가 없어도(예: '상봉역 상봉생활 최초모집공고') 태그를 붙인다.
    if "청년안심" in name or "청년안심주택 포털" in (n.get("house_kind") or ""):
        tags.append("청년안심 — 특공은 소득 120% 초과로 불가, 일반공급으로만 신청")
    if "신혼희망" in name:
        tags.append("⚠ 신혼희망타운 — 수익공유형 확인")
    kind = (n.get("house_kind") or "") + name
    if "행복주택" in kind:
        tags.append("행복주택 — 청년·신혼 대상, 소득요건 확인(맞벌이 완화). 대출규제 무관")
    elif "국민임대" in kind:
        tags.append("국민임대 — 소득·자산 요건, 장기거주(최장 30년). 대출규제 무관")
    elif "공공임대" in kind:  # 통합공공임대 포함
        tags.append("공공임대 — 소득·자산 요건 확인, 장기거주 가능. 대출규제 무관")
    if cfg.get("mode") == "premarital":
        tags.append("예비신혼부부 가능 여부·혼인신고 기한 공고문 확인")
    return tags


# ------------------------------------------------------------ 알림 메시지
def _fmt_range(a, b):
    if a and b and a != b:
        return f"{a} ~ {b}"
    return a or b or "-"


DASHBOARD_URL = "https://example.com/"


def _dash_url() -> str:
    return load_config().get("dashboard_url") or DASHBOARD_URL


def new_notice_message(n: dict) -> str:
    """텔레그램은 최소 정보만: 공고명 · 지역/유형 · 핵심 일정 · 대시보드 링크."""
    lines = [f"🏠 <b>{n['name']}</b>"]
    sub = " · ".join(x for x in (n.get("region"), n.get("house_kind")) if x)
    if sub:
        lines.append(sub)
    when = n.get("sp_start") or n.get("r1_start") or n.get("notice_date")
    if when:
        lines.append(f"접수 {str(when)[5:].replace('-', '.')}~")
    lines.append(_dash_url())
    return "\n".join(lines)


def reminder_message(n: dict, days_left: int, what: str) -> str:
    dday = "오늘 마감" if days_left == 0 else f"D-{days_left}"
    return (
        f"⏰ <b>{dday}: {what}</b>\n"
        f"<b>{n['name']}</b>\n"
        + _dash_url()
    )


def parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def due_reminders(n: dict, today: date, reminder_days: list[int]) -> list[tuple[str, str]]:
    """리마인더 대상이면 [(alert_key_suffix, message)] 반환."""
    out = []
    targets = [
        ("특별공급 접수 시작", parse_date(n.get("sp_start")), "sp_start"),
        ("1순위 접수 시작", parse_date(n.get("r1_start")), "r1_start"),
        ("접수 마감", parse_date(n.get("r1_end")) or parse_date(n.get("sp_end")), "end"),
    ]
    for label, dt, key in targets:
        if not dt:
            continue
        left = (dt - today).days
        if left in reminder_days or left == 0:
            out.append((f"{key}:d{left}", reminder_message(n, left, label)))
    return out
