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
from datetime import datetime, timedelta

import requests

log = logging.getLogger("sources")

APPLYHOME_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
LH_BASE = "http://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"

LOOKBACK_DAYS = 60  # 최근 N일 공고만 조회


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
        try:
            got = fetch_lh(lh_key)
            log.info("fetch_lh: %d건", len(got))
            notices += got
        except Exception as e:  # noqa: BLE001
            log.error("fetch_lh 실패: %s", e)
    else:
        log.warning("LH_API_KEY 미설정 — LH 수집 건너뜀")

    from app.rules import load_config
    notices += fetch_boards(load_config().get("boards"))
    return notices
