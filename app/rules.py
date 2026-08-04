"""필터 + 태깅 규칙.

config.yaml 기준으로 관심 지역을 거르고,
두 사람 상황(서울 2년 거주·각자 세대주·혼인신고 전·생애최초)에 맞는
행동 태그를 공고에 붙인다.
"""
import os
from datetime import date
from urllib.parse import quote

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
    regions = cfg.get("regions") or {}
    if regions.get("seoul") and "서울" in region:
        return True
    if regions.get("gyeonggi_all") and ("경기" in region or "경기도" in region):
        return True  # 경기 전역 허용 — 통근 1시간 초과는 process()의 통근필터가 제외
    for city in regions.get("gyeonggi_cities", []):
        if city in region:
            return True
    return False


def excluded(n: dict, cfg: dict) -> bool:
    name = (n.get("name") or "") + (n.get("house_kind") or "")
    return any(kw in name for kw in cfg.get("exclude_keywords", []))


def _man(won) -> str:
    return f"{won / 10000:.1f}만"


def income_verdict(kind: str, cfg: dict):

    기준은 건강보험 보수월액 합산(profile.health_pay_monthly_won) — 심사 조회 1순위 자료다.
    원천징수 총급여는 조회 ⑤순위라 판정에 쓰지 않는다.
    상한은 config의 income_limit_2p_dual(공고문에서 확인한 절대금액)에서 읽는다.
    해당 유형이 없거나 공고마다 기준이 갈리면 None을 돌려 '분석 필요'로 남긴다."""
    hp = (cfg.get("profile") or {}).get("health_pay_monthly_won") or {}
    total = (hp.get("me") or 0) + (hp.get("spouse") or 0)
    lim = cfg.get("income_limit_2p_dual") or {}
    if not total or not lim:
        return None
    ours, solo = _man(total), max(hp.get("me") or 0, hp.get("spouse") or 0)

    def cmp(cap, label):
        ok = total <= cap
        return f"{'✅' if ok else '❌'} 소득 {ours} {'≤' if ok else '>'} {_man(cap)} — {label}"

    # ★ 판정 순서 주의. 청년안심 포털 게시판 공고는 house_kind에 '청년안심주택 포털'이
    #   들어가서, [민간임대] 공고까지 청년안심(공공임대) 한도로 잘못 판정된다.
    #   공공지원민간임대는 상한이 2인 120%(704.0만)로 공공임대보다 낮으므로 먼저 걸러낸다.
    if "민간임대" in kind and lim.get("공공지원민간임대"):
        # 공공지원민간임대는 특별공급(약 20%)에만 소득·자산 요건이 있고
        # 일반공급(약 80%)은 "소득 및 자산, 지역 요건 없음"인 공고가 많다.
        #   근거: 강변역 강변 스카이타워(2026.07.28) '■ 일반공급 신청자격 ④ 소득 및
        #   자산, 지역 요건 없음' — 신혼부부 67세대가 전부 일반공급이라 소득 무관.
        # 예전엔 공고 전체를 특별공급 기준(2인 120%)으로 ❌ 처리해 신청 가능한 공고를
        # 걸러냈다. 물량의 80%가 요건 없으므로 ✅로 두고 확인 문구를 함께 붙인다.
        cap = lim["공공지원민간임대"]
        return (f"✅ 소득 {ours} — 민간임대 일반공급(약 80%)은 소득·자산 요건 없음 / "
                f"특별공급(20%)은 {_man(cap)} {'초과' if total > cap else '이내'}"
                f" · 공고문에서 일반공급 요건 확인")
    if "행복주택" in kind and lim.get("행복주택"):
        # 초과여도 태아 포함 3인이 되면 기준이 3인 구간으로 올라가 통과한다(과천 공고문 가산표).
        extra = "" if total <= lim["행복주택"] else " 초과 · 태아 포함 3인 되면 가능"
        return cmp(lim["행복주택"], "행복주택 2인 맞벌이" + extra)
    if "청년안심" in kind:
        n2, n1 = lim.get("청년안심_신혼2"), lim.get("청년안심_신혼1")
        y1 = lim.get("청년_1인")
        parts = []
        if n2:
            parts.append(f"신혼Ⅱ {'가능' if total <= n2 else '불가'}(한도 {_man(n2)})")
        if n1:
            parts.append(f"신혼Ⅰ {'가능' if total <= n1 else '불가'}(한도 {_man(n1)})")
        if y1:
            parts.append(f"청년 각자 {'가능' if solo <= y1 else '불가'}(한도 {_man(y1)})")
        mark = "✅" if (n2 and total <= n2) else "❌"
        return f"{mark} 소득 {ours} — " + " / ".join(parts)
    return None


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
        # 신희타 분양은 '3인 이하'를 한 구간으로 묶고 맞벌이 200% 구간이 따로 있어
        tags.insert(0, "✅ 소득 — 신희타 분양은 '3인 이하' 구간 + 맞벌이 200%까지 열려 여유")
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
        # 소득기준은 공고문마다 다르고(1인 120%·2인 110% 등) 아예 없는 공고도 있어
        # 고정 태그로 판정하지 않는다. 정확한 순위는 🔄 LLM 분석(공고문 기준 추출)으로.
        tags.append("청년안심 — 소득기준이 공고마다 다름(없는 공고도 있음). 🔄 분석으로 우리 순위 판정")
    if "신혼희망" in name:
        tags.append("⚠ 신혼희망타운 — 수익공유형 확인")
    # (도시근로자 월평균소득 %는 대략치 — 실제는 가구원수·연도별 공고문 확인)
    kind = (n.get("house_kind") or "") + name
    if "행복주택" in kind:
        # 2인 가구 가산을 빼먹은 값이었다. 실제 과천 신혼희망타운 행복주택 공고문은
        # 이 태그를 믿었으면 자격 있는 공고를 걸러냈다. 출산자녀 가산(+10%p)까지 겹쳐
        # 고정 수치는 계속 틀리므로 중립 문구만 두고 판정은 🔄 LLM 분석에 맡긴다.
        tags.append("행복주택 — 소득기준이 가구원수·맞벌이·출산자녀 가산에 따라 갈림"
                    "(2인 맞벌이는 130%까지 열림). 🔄 분석으로 우리 기준 판정")
    elif "국민임대" in kind:
    elif "통합공공임대" in kind:
    elif "공공임대" in kind:  # 5·10년 공공임대(분양전환)
        tags.append("공공임대(분양전환) — 소득·자산 요건 확인. 향후 분양전환 가능, 대출규제 무관")
    elif "장기전세" in kind:
    if cfg.get("mode") == "premarital":
        tags.append("예비신혼부부 가능 여부·혼인신고 기한 공고문 확인")
    # 소득 판정을 맨 앞에 — 카드를 열지 않고도 되는지/안 되는지 바로 보이게.
    verdict = income_verdict(kind, cfg)
    if verdict:
        tags.insert(0, verdict)
    return tags


# ------------------------------------------------------------ 알림 메시지
def _fmt_range(a, b):
    if a and b and a != b:
        return f"{a} ~ {b}"
    return a or b or "-"


DASHBOARD_URL = "https://example.com/"


def _dash_url() -> str:
    return load_config().get("dashboard_url") or DASHBOARD_URL


def _notice_url(n: dict) -> str:
    """대시보드 링크에 공고 id 프래그먼트(#id=…)를 붙여 클릭 시 해당 공고 카드로 바로 이동."""
    base = _dash_url()
    nid = n.get("id")
    if not nid:
        return base
    return f"{base}#id={quote(str(nid), safe='')}"


def new_notice_message(n: dict) -> str:
    """텔레그램은 최소 정보만: 공고명 · 지역/유형 · 핵심 일정 · 대시보드 링크(해당 공고로 이동)."""
    lines = [f"🏠 <b>{n['name']}</b>"]
    sub = " · ".join(x for x in (n.get("region"), n.get("house_kind")) if x)
    if sub:
        lines.append(sub)
    when = n.get("sp_start") or n.get("r1_start") or n.get("notice_date")
    if when:
        lines.append(f"접수 {str(when)[5:].replace('-', '.')}~")
    lines.append(_notice_url(n))
    return "\n".join(lines)


def reminder_message(n: dict, days_left: int, what: str) -> str:
    dday = "오늘 마감" if days_left == 0 else f"D-{days_left}"
    return (
        f"⏰ <b>{dday}: {what}</b>\n"
        f"<b>{n['name']}</b>\n"
        + _notice_url(n)
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
