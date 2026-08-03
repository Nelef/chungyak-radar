"""게시판형 임대 공고 수집 — 서울시 청년안심주택 포털, SH 서울주택도시개발공사.

이 계열(미리내집·장기전세, 청년안심주택 민간임대 등)은 청약홈·LH API에 안 잡히고
각 기관 게시판에만 올라오므로 목록을 직접 받아온다.

두 게시판은 목록 제공 방식이 완전히 달라 소스별 전용 파서를 쓴다(board["type"]로 분기):
  - type: soco_json  청년안심주택 포털 — 목록이 JS로 그려지고 실제 데이터는
                     bbsListJson.json(AJAX, POST)이 JSON으로 준다. HTML만 긁으면 0건.
  - type: sh_board   SH 공고 — HTML 테이블이지만 글 링크가 <a href="#"
                     onclick="getDetailView('306670')"> 형태라 href만 보면 다 놓친다.
                     onclick의 seq로 view.do?seq= 링크를 만든다.
  - type: anchor(기본) 예전 방식(단순 a[href] 게시판)용 폴백.

게시판 개편으로 파싱이 깨지면 "수집 0건" 경고 로그가 남는다.
그 경우 config.yaml의 boards 섹션에서 URL/타입만 고치면 된다.
MOCK=1이면 샘플 항목을 반환한다.
"""
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("boards")

# config.yaml의 boards 항목이 있으면 그것을 우선 사용 (아래는 폴백 기본값)
DEFAULT_BOARDS = [
    {
        "name": "청년안심주택 포털",
        "type": "soco_json",
        "json_url": "https://soco.seoul.go.kr/youth/pgm/home/yohome/bbsListJson.json",
        "bbs_id": "BMSR00015",
        "view_url": "https://soco.seoul.go.kr/youth/bbs/BMSR00015/view.do?menuNo=400008&boardId={id}",
        "pages": 2,
        "keywords": ["모집공고", "입주자 모집", "청년안심"],
    },
    {
        "name": "SH 공고",
        "type": "sh_board",
        "list_url": "https://www.i-sh.co.kr/main/lay2/program/S1T294C297/www/brd/m_247/list.do",
        "base_url": "https://www.i-sh.co.kr",
        "keywords": ["미리내집", "장기전세", "신혼", "청년안심", "입주자 모집", "모집공고"],
    },
]

DATE_RE = re.compile(r"20\d{2}[.\-/]\s?\d{1,2}[.\-/]\s?\d{1,2}")
HEADERS = {"User-Agent": "Mozilla/5.0 (chungyak-radar; personal use)"}
AJAX_HEADERS = {**HEADERS, "X-Requested-With": "XMLHttpRequest"}


def _norm_date(s: str):
    m = DATE_RE.search(s or "")
    if not m:
        return None
    parts = re.split(r"[.\-/]", m.group(0))
    try:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    except (ValueError, IndexError):
        return None


def _keyword_ok(board: dict, title: str) -> bool:
    kws = board.get("keywords")
    return (not kws) or any(kw in title for kw in kws)


# 공고 본문에서 접수기간을 뽑기 위한 라벨/날짜 패턴.
# 실제 표기 예: "■ 청약 접수일 : 2026. 08. 11.(화) 10:00 ~ 2026. 08. 13.(목) 17:00"
_APPLY_LABELS = ("모집신청일", "접수일", "접수기간", "신청기간", "접수 기간", "청약접수",
                 "청약 접수", "신청 접수", "접수일정", "인터넷접수", "인터넷 접수")
# 접수기간이 아닌 다른 일정 라벨. 라벨과 첫 날짜 사이에 이 말이 끼면 그 창은 버린다.
# (예전에 안내문 "※ 청약 접수 시작 시간과 마감 시간에는…" 뒤의 '서류심사대상자 발표
#  2026.08.21 / 서류제출기간 08.31'을 접수기간으로 잡아 D-day가 10일씩 틀렸다)
_NOT_APPLY = ("서류심사", "서류제출", "제출기간", "발표", "당첨", "계약", "입주지정", "소명", "추첨")
_DATE_ANY = re.compile(r"20\d{2}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}")


def _iso_date(s: str):
    n = re.findall(r"\d+", s)
    try:
        return f"{int(n[0]):04d}-{int(n[1]):02d}-{int(n[2]):02d}"
    except (IndexError, ValueError):
        return None


def _sh_apply_period(html: str):
    """공고 본문(SH 상세 HTML 또는 청년안심 포털 JSON의 content)에서 접수기간(시작, 마감) 추출.

    라벨 후보를 전부 훑어 '라벨 → 첫 날짜' 거리가 가장 짧은 것을 고른다. 첫 매칭만
    쓰면 본문 안내문에 걸려 엉뚱한 일정을 접수기간으로 잡는다. 본문에 없으면
    (공고문 PDF에만 있거나 상시모집) (None, None)."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))
    best = None  # (라벨~첫날짜 거리, 시작, 마감)
    for lab in _APPLY_LABELS:
        for m in re.finditer(re.escape(lab), text):
            win = text[m.end():m.end() + 140]
            dm = _DATE_ANY.search(win)
            if not dm:
                continue
            if any(k in win[:dm.start()] for k in _NOT_APPLY):
                continue  # 서류제출·발표 등 접수기간이 아닌 일정
            ds = [d for d in (_iso_date(x) for x in _DATE_ANY.findall(win)) if d]
            if not ds:
                continue
            start = ds[0]
            end = next((d for d in ds[1:] if d >= start), None)  # 시작보다 빠른 날짜는 종료로 안 씀
            if best is None or dm.start() < best[0]:
                best = (dm.start(), start, end)
    return (best[1], best[2]) if best else (None, None)


def _notice(board: dict, uid: str, title: str, link: str, posted, address="서울") -> dict:
    return {
        "id": f"board:{uid}",
        "source": "board",
        "name": title,
        "house_kind": f"임대 공고 ({board['name']})",
        "region": "서울",
        "address": address,
        "notice_date": posted,
        "url": link,
        "flags": {"임대": True},
        "raw": {"board": board["name"]},
    }


# --------------------------------------------- 청년안심주택 포털 (JSON AJAX)
def _scrape_soco_json(board: dict) -> list[dict]:
    """soco.seoul.go.kr 목록은 bbsListJson.json이 JSON으로 내려준다(제목=nttSj)."""
    out = []
    pages = int(board.get("pages", 1))
    for pg in range(1, pages + 1):
        payload = {
            "bbsId": board.get("bbs_id", "BMSR00015"),
            "pageIndex": pg,
            "searchAdresGu": "",
            "searchCondition": "",
            "searchKeyword": "",
            "optn2": "",
            "optn5": "",
        }
        r = requests.post(board["json_url"], data=payload, headers=AJAX_HEADERS, timeout=30)
        r.raise_for_status()
        rows = (r.json() or {}).get("resultList") or []
        if not rows:
            break
        for it in rows:
            title = " ".join(str(it.get("nttSj") or "").split())
            if len(title) < 4 or not _keyword_ok(board, title):
                continue
            bid = it.get("boardId")
            link = board["view_url"].format(id=bid) if bid else board["json_url"]
            posted = _norm_date(str(it.get("optn1") or it.get("regDate") or ""))
            n = _notice(board, f"soco:{bid}", title, link, posted)
            n["r1_start"] = _norm_date(str(it.get("optn4") or ""))  # 신청 접수 시작일
            # 목록 JSON의 content에 본문 HTML이 그대로 담겨 있어 접수 '마감일'까지 읽을 수 있다.
            # (optn5는 항상 null이라 예전엔 마감 리마인더가 안 붙었음 — TODO 5)
            s, e = _sh_apply_period(str(it.get("content") or ""))
            if s:
                n["r1_start"] = s
            if e:
                n["r1_end"] = e
            n["raw"]["atchFileId"] = it.get("atchFileId")  # 공고문 PDF 첨부 ID
            n["raw"]["boardId"] = bid
            out.append(n)
    return out


# --------------------------------------------------- SH 공고 (HTML 테이블)
def _sh_view_url(list_url: str, seq: str) -> str:
    # .../m_247/list.do  ->  .../m_247/view.do?seq=SEQ
    base = list_url.split("list.do")[0] if "list.do" in list_url else list_url.rsplit("/", 1)[0] + "/"
    return f"{base}view.do?seq={seq}"


def _scrape_sh_board(board: dict) -> list[dict]:
    """SH 글 링크는 onclick=getDetailView('seq'); seq로 view.do?seq= 링크를 만든다.
    목록은 ?page=N으로 넘긴다(기본 1페이지=파라미터 없음). 접수 중인데 1페이지에서
    밀려난 공고까지 잡으려면 pages를 늘린다(config boards.pages)."""
    pages = int(board.get("pages", 2))
    out, seen = [], set()
    for pg in range(1, pages + 1):
        # isRecrnoti=Y = SH '접수중 모집공고'만 준다(발표·경쟁률·주택공개 자동 제외,
        # 목록 1페이지에서 밀려난 접수중 공고도 회수). view.do 링크는 list_url(쿼리 없음)로 만든다.
        url = f"{board['list_url']}?isRecrnoti=Y" + (f"&page={pg}" if pg > 1 else "")
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("SH 목록 %d페이지 실패: %s", pg, e)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[onclick]"):
            m = re.search(r"getDetailView\('?(\d+)'?\)", a.get("onclick", ""))
            if not m:
                continue
            seq = m.group(1)
            title = " ".join(a.get_text(" ", strip=True).replace("NEW", "").split())
            if len(title) < 6 or seq in seen or not _keyword_ok(board, title):
                continue
            seen.add(seq)
            row = a.find_parent("tr")
            posted = _norm_date(row.get_text(" ", strip=True)) if row else None
            view = _sh_view_url(board["list_url"], seq)
            n = _notice(board, f"sh:{seq}", title, view, posted)
            # 상세 본문에서 접수기간을 뽑아 D-day/리마인더가 붙게 한다(LLM 불필요).
            # 목록에는 접수일정이 없어 상세를 열어야 함. 실패해도 제목/링크는 유지.
            try:
                dr = requests.get(view, headers=HEADERS, timeout=12)
                dr.raise_for_status()
                s, e = _sh_apply_period(dr.text)
                if s:
                    n["r1_start"], n["r1_end"] = s, e
            except Exception as exc:  # noqa: BLE001
                log.debug("SH 상세 접수기간 파싱 실패(%s): %s", seq, exc)
            out.append(n)
    return out


# ------------------------------------------- 단순 a[href] 게시판 (폴백)
def _scrape_anchor_board(board: dict) -> list[dict]:
    r = requests.get(board["list_url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href]"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = a.get("href", "")
        if len(title) < 8 or href.startswith(("#", "javascript")):
            continue
        if not _keyword_ok(board, title):
            continue
        link = href if href.startswith("http") else board["base_url"] + (href if href.startswith("/") else "/" + href)
        key = hashlib.sha1((board["name"] + title).encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        row = a.find_parent(["tr", "li", "div"])
        posted = _norm_date(row.get_text(" ", strip=True)) if row else None
        out.append(_notice(board, key, title, link, posted))
    return out


_SCRAPERS = {
    "soco_json": _scrape_soco_json,
    "sh_board": _scrape_sh_board,
    "anchor": _scrape_anchor_board,
}


def _mock_boards() -> list[dict]:
    d = lambda n: (datetime.now() + timedelta(days=n)).strftime("%Y-%m-%d")
    return [
        {
            "id": "board:mock-mirinae",
            "source": "board",
            "name": "제8차 장기전세주택2(미리내집) 입주자 모집공고 (샘플)",
            "house_kind": "임대 공고 (SH 공고)",
            "region": "서울", "address": "서울",
            "notice_date": d(-1),
            "url": "https://www.i-sh.co.kr",
            "flags": {"임대": True}, "raw": {},
        },
        {
            "id": "board:mock-sangbong",
            "source": "board",
            "name": "[민간임대] 상봉역 상봉생활 최초모집공고 (샘플)",
            "house_kind": "임대 공고 (청년안심주택 포털)",
            "region": "서울", "address": "서울 중랑구 상봉동",
            "notice_date": d(-4),
            "url": "https://soco.seoul.go.kr/youth/bbs/BMSR00015/view.do?menuNo=400008&boardId=6585",
            "flags": {"임대": True}, "raw": {},
        },
    ]


def fetch_boards(boards: list[dict] | None = None) -> list[dict]:
    if os.environ.get("MOCK") == "1":
        return _mock_boards()

    notices = []
    for board in (boards or DEFAULT_BOARDS):
        scraper = _SCRAPERS.get(board.get("type", "anchor"))
        if scraper is None:
            log.error("%s: 알 수 없는 board type=%s — 건너뜀", board.get("name"), board.get("type"))
            continue
        try:
            got = scraper(board)
            if not got:
                log.warning("%s: 수집 0건 — 게시판 개편 가능성, config의 URL/타입 확인 필요", board["name"])
            else:
                log.info("%s: %d건", board["name"], len(got))
            notices += got
        except Exception as e:  # noqa: BLE001
            log.error("%s 수집 실패: %s", board["name"], e)
    return notices
