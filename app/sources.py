"""공고 수집기.

소스 1) 청약홈 — 공공데이터포털 '한국부동산원_주택청약 분양정보 조회 서비스'
        https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail
소스 2) LH청약플러스 — 공공데이터포털 '한국토지주택공사_분양임대공고문 정보'
        http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1

두 API 모두 data.go.kr에서 활용신청(자동승인) 후 발급받는 인증키 필요.
MOCK=1 환경변수를 주면 실제 호출 없이 샘플 데이터로 파이프라인을 검증할 수 있다.
필드명이 기관 사정으로 바뀌는 경우가 있어 전부 .get()으로 방어적으로 읽는다.
"""
import logging
import os
import re
import ssl
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

log = logging.getLogger("sources")

APPLYHOME_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
LH_BASE = "http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
# GH 경기주택도시공사 청약센터 (행복주택·국민임대 등 경기도 자체 공급)
GH_LIST = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do"
GH_DETAIL = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancDetailView.do"
# 우리(예비신혼·청년, 무주택) 자격 대상 임대 유형만. 영구임대(취약계층)·매입/전세임대 제외.
RENT_TYPES = ("행복주택", "국민임대", "공공임대", "통합공공임대", "6년 공공임대", "장기전세")
UA = {"User-Agent": "Mozilla/5.0"}

LOOKBACK_DAYS = 60  # 최근 N일 공고만 조회


class _TLSAdapter(HTTPAdapter):
    """일부 정부/공사 사이트(GH 등)가 기본 SECLEVEL/legacy 재협상을 거부해 핸드셰이크가 실패한다.
    SECLEVEL 완화 + legacy 옵션으로 접속 가능하게 한다(공개 목록 조회용)."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        try:
            ctx.options |= 0x4  # OP_LEGACY_SERVER_CONNECT
        except Exception:  # noqa: BLE001
            pass
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


# ---------------------------------------------------------------- 청약홈 APT
def fetch_applyhome(service_key: str) -> list[dict]:
    """APT 분양정보 상세 → 표준 공고 dict 목록."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "page": 1,
        "perPage": 300,
        "serviceKey": service_key,
        "cond[RCRIT_PBLANC_DE::GTE]": since,
    }
    r = requests.get(f"{APPLYHOME_BASE}/getAPTLttotPblancDetail", params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("data", [])
    out = []
    for it in items:
        no = _get(it, "PBLANC_NO", "HOUSE_MANAGE_NO")
        if not no:
            continue
        out.append({
            "id": f"applyhome:{no}",
            "source": "applyhome",
            "name": _get(it, "HOUSE_NM", default="(이름 없음)"),
            "house_kind": _get(it, "HOUSE_DTL_SECD_NM", "HOUSE_SECD_NM"),
            "region": _get(it, "SUBSCRPT_AREA_CODE_NM"),
            "address": _get(it, "HSSPLY_ADRES"),
            "total_supply": _get(it, "TOT_SUPLY_HSHLDCO"),
            "notice_date": _get(it, "RCRIT_PBLANC_DE"),
            "sp_start": _get(it, "SPSPLY_RCEPT_BGNDE"),
            "sp_end": _get(it, "SPSPLY_RCEPT_ENDDE"),
            "r1_start": _get(it, "GNRL_RNK1_CRSPAREA_RCPTDE", "RCEPT_BGNDE"),
            "r1_end": _get(it, "GNRL_RNK1_ETC_AREA_ENDDE", "GNRL_RNK1_CRSPAREA_ENDDE"),
            "r2_start": _get(it, "GNRL_RNK2_CRSPAREA_RCPTDE"),
            "r2_end": _get(it, "GNRL_RNK2_ETC_AREA_ENDDE", "GNRL_RNK2_CRSPAREA_ENDDE"),
            "winner_date": _get(it, "PRZWNER_PRESNATN_DE"),
            "url": _get(it, "PBLANC_URL"),
            "flags": {
                "투기과열": _get(it, "SPECLT_RDN_EARTH_AT") == "Y",
                "조정대상": str(_get(it, "MDAT_TRGET_AREA_SECD", default="")).startswith("Y") or _get(it, "MDAT_TRGET_AREA_SECD") == "Y",
                "분양가상한제": _get(it, "PARCPRC_ULS_AT") == "Y",
                "공공주택지구": _get(it, "PUBLIC_HOUSE_EARTH_AT") == "Y",
                "대규모택지": _get(it, "LRSCL_BLDLND_AT") == "Y",
            },
            "raw": it,
        })
    return out


# ------------------------------------------------------- 청약홈 무순위/잔여
def fetch_applyhome_remndr(service_key: str) -> list[dict]:
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "page": 1,
        "perPage": 200,
        "serviceKey": service_key,
        "cond[RCRIT_PBLANC_DE::GTE]": since,
    }
    r = requests.get(f"{APPLYHOME_BASE}/getRemndrLttotPblancDetail", params=params, timeout=30)
    r.raise_for_status()
    items = r.json().get("data", [])
    out = []
    for it in items:
        no = _get(it, "PBLANC_NO", "HOUSE_MANAGE_NO")
        if not no:
            continue
        out.append({
            "id": f"applyhome_remndr:{no}",
            "source": "applyhome_remndr",
            "name": "[무순위] " + _get(it, "HOUSE_NM", default="(이름 없음)"),
            "house_kind": _get(it, "HOUSE_DTL_SECD_NM", "HOUSE_SECD_NM", default="무순위/잔여"),
            "region": _get(it, "SUBSCRPT_AREA_CODE_NM"),
            "address": _get(it, "HSSPLY_ADRES"),
            "total_supply": _get(it, "TOT_SUPLY_HSHLDCO"),
            "notice_date": _get(it, "RCRIT_PBLANC_DE"),
            "r1_start": _get(it, "SUBSCRPT_RCEPT_BGNDE", "RCEPT_BGNDE"),
            "r1_end": _get(it, "SUBSCRPT_RCEPT_ENDDE", "RCEPT_ENDDE"),
            "winner_date": _get(it, "PRZWNER_PRESNATN_DE"),
            "url": _get(it, "PBLANC_URL"),
            "flags": {},
            "raw": it,
        })
    return out


# --------------------------------------------------- 공공지원 민간임대 (청약홈)
def fetch_pbl_pvt_rent(service_key: str) -> list[dict]:
    """공공지원 민간임대 분양(임대)정보. 접수기간이 API에 있어 날짜가 바로 붙는다."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    params = {
        "page": 1, "perPage": 300, "serviceKey": service_key,
        "cond[RCRIT_PBLANC_DE::GTE]": since,
    }
    r = requests.get(f"{APPLYHOME_BASE}/getPblPvtRentLttotPblancDetail", params=params, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json().get("data", []):
        no = _get(it, "PBLANC_NO", "HOUSE_MANAGE_NO")
        if not no:
            continue
        out.append({
            "id": f"applyhome_rent:{no}",
            "source": "applyhome_rent",
            "name": "[민간임대] " + _get(it, "HOUSE_NM", default="(이름 없음)"),
            "house_kind": "공공지원 민간임대",
            "region": _get(it, "SUBSCRPT_AREA_CODE_NM"),
            "address": _get(it, "HSSPLY_ADRES"),
            "total_supply": _get(it, "TOT_SUPLY_HSHLDCO"),
            "notice_date": _fmt_lh_date(_get(it, "RCRIT_PBLANC_DE")),
            "r1_start": _fmt_lh_date(_get(it, "SUBSCRPT_RCEPT_BGNDE")),
            "r1_end": _fmt_lh_date(_get(it, "SUBSCRPT_RCEPT_ENDDE")),
            "winner_date": _fmt_lh_date(_get(it, "PRZWNER_PRESNATN_DE")),
            "url": _get(it, "PBLANC_URL"),
            "flags": {"임대": True},
            "raw": it,
        })
    return out


# --------------------------------------------------------------------- LH
def fetch_lh(service_key: str) -> list[dict]:
    """LH 분양주택 공고 (뉴:홈, 3기 신도시 본청약, 신혼희망타운이 이 채널로 나옴)."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    out = []
    for cnp in ("11", "41"):  # 서울, 경기
        params = {
            "serviceKey": service_key,
            "PG_SZ": 100,
            "PAGE": 1,
            "UPP_AIS_TP_CD": "05",  # 분양주택
            # 공고게시일 필터. 실물 검증 결과 PAN_NT_ST_DT는 무시되고
            # PAN_ST_DT/PAN_ED_DT가 실제 필터라 이 둘로 최근 LOOKBACK_DAYS 범위를 지정.
            "PAN_ST_DT": since,
            "PAN_ED_DT": today,
            "CNP_CD": cnp,
        }
        r = requests.get(LH_BASE, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        # LH 응답은 [[{resHeader}], [{"dsList":[...]}]] 형태. 방어적으로 파싱.
        rows = []
        if isinstance(body, list):
            for part in body:
                if isinstance(part, list):
                    for p in part:
                        if isinstance(p, dict) and "dsList" in p:
                            rows = p["dsList"] or []
                elif isinstance(part, dict) and "dsList" in part:
                    rows = part["dsList"] or []
        elif isinstance(body, dict):
            rows = body.get("dsList", [])
        for it in rows:
            pan_id = _get(it, "PAN_ID")
            if not pan_id:
                continue
            out.append({
                "id": f"lh:{pan_id}",
                "source": "lh",
                "name": _get(it, "PAN_NM", default="(이름 없음)"),
                "house_kind": _get(it, "AIS_TP_CD_NM", "UPP_AIS_TP_NM", default="분양주택"),
                "region": _get(it, "CNP_CD_NM"),
                "address": _get(it, "CNP_CD_NM"),
                "notice_date": _fmt_lh_date(_get(it, "PAN_NT_ST_DT")),
                "r1_end": _fmt_lh_date(_get(it, "CLSG_DT")),  # 공고 마감일을 접수 마감으로 취급
                "url": _get(it, "DTL_URL"),
                "flags": {"상태": _get(it, "PAN_SS", default="")},
                "raw": it,
            })
    return out


def _lh_dslist(body) -> list:
    """LH API 응답([[{resHeader}],[{dsList}]] 등)에서 dsList를 방어적으로 추출."""
    if isinstance(body, list):
        for part in body:
            for p in (part if isinstance(part, list) else [part]):
                if isinstance(p, dict) and "dsList" in p:
                    return p["dsList"] or []
    elif isinstance(body, dict):
        return body.get("dsList", [])
    return []


def _lh_detail(url) -> dict:
    """LH 임대 상세페이지에서 소재지·전용면적을 읽는다.
    임대 API는 주소가 '경기도'처럼 광역뿐이라 통근 계산에 상세의 정확한 주소가 필요하다.
    형식: <li class="w100">소재지 : 경기도 성남시 수정구 금토동 194-1</li>"""
    if not url:
        return {}
    try:
        r = requests.get(url, headers=UA, timeout=25)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("LH 임대 상세 실패(%s): %s", url[:60], e)
        return {}
    txt = re.sub(r"<[^>]+>", " ", r.text)
    txt = re.sub(r"\s+", " ", txt)
    out = {}
    m = re.search(r"소재지\s*:\s*([^:<]{4,60}?)\s*(?:전용면적|총 세대수|난방|입주|$)", txt)
    if m:
        out["address"] = m.group(1).strip()
    m = re.search(r"전용면적\(㎡\)\s*:\s*([\d.~\s]+)", txt)
    if m:
        out["area_text"] = m.group(1).strip()
    m = re.search(r"총 세대수\s*:\s*(\d+)", txt)
    if m:
        out["total_supply"] = int(m.group(1))
    return out


def fetch_lh_lease(service_key: str) -> list[dict]:
    """LH 임대 공고(행복주택·국민임대·공공임대 등). 분양(fetch_lh)과 같은 API의 임대(UPP=06)."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    out = []
    for cnp in ("11", "41"):  # 서울, 경기
        params = {
            "serviceKey": service_key, "PG_SZ": 100, "PAGE": 1,
            "UPP_AIS_TP_CD": "06",  # 임대주택
            "PAN_ST_DT": since, "PAN_ED_DT": today, "CNP_CD": cnp,
        }
        r = requests.get(LH_BASE, params=params, timeout=30)
        r.raise_for_status()
        for it in _lh_dslist(r.json()):
            pan_id = _get(it, "PAN_ID")
            kind = _get(it, "AIS_TP_CD_NM", "UPP_AIS_TP_NM", default="")
            if not pan_id or not any(k in kind for k in RENT_TYPES):
                continue  # 영구임대·매입/전세임대 등 대상 외 유형 제외
            pan_nm = _get(it, "PAN_NM", default="(이름 없음)")
            url = _get(it, "DTL_URL")
            det = _lh_detail(url)  # 소재지·전용면적(통근 계산에 필요)
            it = {**it, "area_text": det.get("area_text")}
            out.append({
                "id": f"lh_rent:{pan_id}",
                "source": "lh_rent",
                "name": f"[{kind}] " + pan_nm,
                "house_kind": kind,
                "region": _get(it, "CNP_CD_NM"),
                "address": det.get("address") or _get(it, "CNP_CD_NM"),
                "total_supply": det.get("total_supply"),
                "notice_date": _fmt_lh_date(_get(it, "PAN_NT_ST_DT")),
                "r1_start": _fmt_lh_date(_get(it, "PAN_NT_ST_DT")),  # 접수시작 별도 필드 없어 공고일로
                "r1_end": _fmt_lh_date(_get(it, "CLSG_DT")),
                "url": url,
                "flags": {"임대": True},
                "raw": it,
            })
    return out


def gh_session() -> requests.Session:
    s = requests.Session()
    s.mount("https://", _TLSAdapter())
    return s


def _gh_detail(sess, pno, knd, biz) -> dict:
    """GH 상세페이지에서 소재지(통근 계산용)·전용면적을 읽는다.
    목록에는 시(市)까지만 있어 지오코딩이 실패하므로 상세의 정확한 주소가 필요하다."""
    try:
        r = sess.get(GH_DETAIL, params={"previewYn": "N", "pbancNo": pno,
                                        "pbancKndCd": knd, "bizTyNm": biz},
                     headers=UA, timeout=25, verify=False)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("GH 상세 실패(pbancNo=%s): %s", pno, e)
        return {}
    out = {}
    for m in re.finditer(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", r.text, re.S):
        th = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        td = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if th == "소재지" and td and "address" not in out:
            out["address"] = td
        elif th.startswith("전용면적") and td:
            out["area_text"] = td
        elif th == "모집호수" and td.isdigit():
            out["total_supply"] = int(td)
    return out


def fetch_gh() -> list[dict]:
    """GH 경기주택도시공사 청약센터 임대 공고(행복주택·국민임대 등, 경기도 자체 공급).
    apply.gh.or.kr 목록 HTML을 파싱. TLS 핸드셰이크가 까다로워 _TLSAdapter로 접속한다."""
    s = gh_session()
    r = s.get(GH_LIST, headers=UA, timeout=25, verify=False)
    r.raise_for_status()
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", r.text, re.S):
        m = re.search(r'data-pbancNo="(\d+)"[^>]*data-pbancKndCd="(\d+)"[^>]*data-bizTyNm="([^"]*)"', row, re.S)
        if not m:
            continue
        pno, knd, biz = m.groups()
        if not any(k in biz for k in RENT_TYPES):
            continue
        tds = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x)).strip()
               for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        tds = [x for x in tds if x]
        name = max((x for x in tds if "공고" in x or "모집" in x), key=len, default=biz)
        region = next((x for x in tds if re.fullmatch(r"[가-힣]+(시|군|구)", x)), "경기")
        dates = re.findall(r"\d{4}[.\-]\d{2}[.\-]\d{2}", row)
        det = _gh_detail(s, pno, knd, biz)  # 소재지·전용면적(통근 계산에 필요)
        out.append({
            "id": f"gh:{pno}",
            "source": "gh",
            "name": f"[{biz}] " + name.replace("&#039;", "'"),
            "house_kind": biz,
            "region": region,
            "address": det.get("address") or f"경기도 {region}",
            "total_supply": det.get("total_supply"),
            "r1_start": dates[0].replace(".", "-") if dates else None,
            "r1_end": dates[1].replace(".", "-") if len(dates) > 1 else None,
            "url": f"{GH_DETAIL}?previewYn=N&pbancNo={pno}&pbancKndCd={knd}&bizTyNm={biz}",
            "flags": {"임대": True},
            "raw": {"pbancNo": pno, "pbancKndCd": knd, "bizTyNm": biz,
                    "area_text": det.get("area_text")},
        })
    return out


def _fmt_lh_date(v):
    if not v:
        return None
    s = str(v).replace(".", "-").replace("/", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


# ------------------------------------------------------------------- MOCK
def fetch_mock() -> list[dict]:
    """API 키 없이 파이프라인 검증용 샘플 3건 (실제 공고 구조와 동일)."""
    today = datetime.now()
    d = lambda n: (today + timedelta(days=n)).strftime("%Y-%m-%d")
    return [
        {
            "id": "mock:2026820005",
            "source": "lh",
            "name": "역곡지구 하우스토리 (부천역곡 A-2블록) 신혼희망타운",
            "house_kind": "공공분양(신혼희망타운)",
            "region": "경기",
            "address": "경기도 부천시 역곡동",
            "total_supply": 976,
            "notice_date": d(-15),
            "r1_start": d(2), "r1_end": d(11),
            "r2_start": d(12), "r2_end": d(12),
            "winner_date": d(24),
            "url": "https://apply.lh.or.kr",
            "flags": {"분양가상한제": True, "공공주택지구": True},
            "raw": {},
        },
        {
            "id": "mock:hanam-a9",
            "source": "lh",
            "name": "하남교산 A9블록 공공분양 본청약 (샘플)",
            "house_kind": "공공분양",
            "region": "경기",
            "address": "경기도 하남시 교산동",
            "total_supply": 1100,
            "notice_date": d(-3),
            "sp_start": d(20), "sp_end": d(20),
            "r1_start": d(21), "r1_end": d(21),
            "winner_date": d(40),
            "url": "https://apply.lh.or.kr",
            "flags": {"투기과열": True, "분양가상한제": True, "대규모택지": True},
            "raw": {},
        },
        {
            "id": "mock:seoul-min",
            "source": "applyhome",
            "name": "서울 성동구 ○○ 민영 분양 (샘플)",
            "house_kind": "민영",
            "region": "서울",
            "address": "서울특별시 성동구",
            "total_supply": 480,
            "notice_date": d(-1),
            "sp_start": d(9), "sp_end": d(9),
            "r1_start": d(10), "r1_end": d(10),
            "r2_start": d(11), "r2_end": d(11),
            "winner_date": d(17),
            "url": "https://www.applyhome.co.kr",
            "flags": {"투기과열": True, "조정대상": True, "분양가상한제": True},
            "raw": {},
        },
    ]


def collect_all() -> list[dict]:
    """활성화된 소스 전부에서 수집. 실패한 소스는 건너뛰고 로그만 남긴다."""
    from app.boards import fetch_boards

    if os.environ.get("MOCK") == "1":
        log.info("MOCK 모드: 샘플 데이터 사용")
        return fetch_mock() + fetch_boards()

    notices: list[dict] = []
    ah_key = os.environ.get("APPLYHOME_API_KEY", "").strip()
    lh_key = os.environ.get("LH_API_KEY", "").strip()

    if ah_key:
        for fn in (fetch_applyhome, fetch_applyhome_remndr, fetch_pbl_pvt_rent):
            try:
                got = fn(ah_key)
                log.info("%s: %d건", fn.__name__, len(got))
                notices += got
            except Exception as e:  # noqa: BLE001
                log.error("%s 실패: %s", fn.__name__, e)
    else:
        log.warning("APPLYHOME_API_KEY 미설정 — 청약홈 수집 건너뜀")

    if lh_key:
        for fn in (fetch_lh, fetch_lh_lease):  # 분양(05) + 임대(06: 행복주택·국민임대 등)
            try:
                got = fn(lh_key)
                log.info("%s: %d건", fn.__name__, len(got))
                notices += got
            except Exception as e:  # noqa: BLE001
                log.error("%s 실패: %s", fn.__name__, e)
    else:
        log.warning("LH_API_KEY 미설정 — LH 수집 건너뜀")

    try:  # GH 경기주택도시공사 (키 불필요, 목록 크롤)
        got = fetch_gh()
        log.info("fetch_gh: %d건", len(got))
        notices += got
    except Exception as e:  # noqa: BLE001
        log.error("fetch_gh 실패: %s", e)

    from app.rules import load_config
    notices += fetch_boards(load_config().get("boards"))
    return notices
