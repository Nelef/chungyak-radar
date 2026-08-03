"""공고 수집기.

소스 1) 청약홈 — 공공데이터포털 '한국부동산원_주택청약 분양정보 조회 서비스'
        https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancDetail
소스 2) LH청약플러스 — 공공데이터포털 '한국토지주택공사_분양임대공고문 정보'
        https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1
        상위유형(UPP_AIS_TP_CD): 05 분양주택 / 06 임대주택 / 39 신혼희망타운
        ※ 39는 '공공분양(신혼희망)'과 '행복주택(신혼희망)'이 섞여 나오므로
          유형명에 '분양'이 있으면 분양, 없으면 임대로 갈라서 넣는다.

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
# https 필수. http로 호출하면 간헐적으로 응답이 오지 않아 ReadTimeout으로 죽는다(2026-08-03 확인).
LH_BASE = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
LH_UPP_SALE = ("05", "39")   # 분양주택 + 신혼희망타운(공공분양)
LH_UPP_RENT = ("06", "39")   # 임대주택 + 신혼희망타운(행복주택)
LH_TIMEOUT = 60
LH_RETRIES = 3
LH_MAX_PAGES = 10            # 폭주 방지 상한 (PG_SZ=100 → 최대 1000건/조합)
# GH 경기주택도시공사 청약센터 (행복주택·국민임대 등 경기도 자체 공급)
GH_LIST = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do"
GH_DETAIL = "https://apply.gh.or.kr/sb/sr/sr7150/selectPbancDetailView.do"
# 수집할 LH·GH 임대 유형 기본값. 실제 값은 config.yaml의 lh_gh_rent_types에서 관리
# (소득요건 초과 유형 제외 — 소득·가구원수가 바뀌면 config만 고치면 된다).
RENT_TYPES = ("통합공공임대", "공공임대", "6년 공공임대", "장기전세")
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
def _lh_fetch_rows(service_key: str, upp: str, cnp: str, since: str, today: str) -> list:
    """LH API 한 조합(상위유형×지역)을 페이지 끝까지 읽어 dsList를 모아 반환.
    타임아웃 등 일시 오류는 재시도하고, 끝까지 실패하면 예외를 올려 호출자가 로그를 남긴다."""
    rows: list = []
    for page in range(1, LH_MAX_PAGES + 1):
        params = {
            "serviceKey": service_key,
            "PG_SZ": 100,
            "PAGE": page,
            "UPP_AIS_TP_CD": upp,
            # 공고게시일 필터. 실물 검증 결과 PAN_NT_ST_DT는 무시되고
            # PAN_ST_DT/PAN_ED_DT가 실제 필터라 이 둘로 최근 LOOKBACK_DAYS 범위를 지정.
            "PAN_ST_DT": since,
            "PAN_ED_DT": today,
            "CNP_CD": cnp,
        }
        got, last_err = None, None
        for attempt in range(1, LH_RETRIES + 1):
            try:
                r = requests.get(LH_BASE, params=params, timeout=LH_TIMEOUT)
                r.raise_for_status()
                got = _lh_dslist(r.json())
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning("LH 조회 실패(upp=%s cnp=%s page=%d, %d/%d회): %s",
                            upp, cnp, page, attempt, LH_RETRIES, e)
        if got is None:
            raise RuntimeError(f"LH 조회 실패(upp={upp} cnp={cnp} page={page}): {last_err}")
        rows += got
        if len(got) < 100:  # 마지막 페이지
            break
    else:
        log.warning("LH 페이지 상한(%d) 도달 — upp=%s cnp=%s, 이후 페이지 누락 가능",
                    LH_MAX_PAGES, upp, cnp)
    return rows


def fetch_lh(service_key: str) -> list[dict]:
    """LH 분양주택 공고 (뉴:홈, 3기 신도시 본청약).
    UPP=05(분양주택)와 UPP=39(신혼희망타운) 중 '공공분양(신혼희망)' 계열을 모은다."""
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    out, seen = [], set()
    for upp in LH_UPP_SALE:
        for cnp in ("11", "41"):  # 서울, 경기
            for it in _lh_fetch_rows(service_key, upp, cnp, since, today):
                pan_id = _get(it, "PAN_ID")
                if not pan_id or pan_id in seen:
                    continue
                kind = _get(it, "AIS_TP_CD_NM", "UPP_AIS_TP_NM", default="분양주택")
                if upp == "39" and "분양" not in kind:
                    continue  # 신혼희망타운 행복주택은 fetch_lh_lease가 담당
                seen.add(pan_id)
                out.append({
                    "id": f"lh:{pan_id}",
                    "source": "lh",
                    "name": _get(it, "PAN_NM", default="(이름 없음)"),
                    "house_kind": kind,
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


def fetch_lh_lease(service_key: str, types=None) -> list[dict]:
    """LH 임대 공고. UPP=06(임대주택) + UPP=39(신혼희망타운) 중 행복주택 계열.
    types에 든 유형만 수집(기본 RENT_TYPES).
    ※ '행복주택(신혼희망)'은 types의 '행복주택'에 부분일치로 걸린다."""
    types = tuple(types or RENT_TYPES)
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    out, seen = [], set()
    for upp in LH_UPP_RENT:
        for cnp in ("11", "41"):  # 서울, 경기
            for it in _lh_fetch_rows(service_key, upp, cnp, since, today):
                pan_id = _get(it, "PAN_ID")
                kind = _get(it, "AIS_TP_CD_NM", "UPP_AIS_TP_NM", default="")
                if not pan_id or pan_id in seen:
                    continue
                if upp == "39" and "분양" in kind:
                    continue  # 신혼희망타운 공공분양은 fetch_lh가 담당
                if not any(k in kind for k in types):
                    continue  # 소득요건 초과·대상 외 유형(국민임대·영구임대 등) 제외
                seen.add(pan_id)
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


def fetch_gh(types=None) -> list[dict]:
    """GH 경기주택도시공사 청약센터 임대 공고(경기도 자체 공급). types에 든 유형만 수집.
    apply.gh.or.kr 목록 HTML을 파싱. TLS 핸드셰이크가 까다로워 _TLSAdapter로 접속한다."""
    types = tuple(types or RENT_TYPES)
    s = gh_session()
    r = s.get(GH_LIST, headers=UA, timeout=25, verify=False)
    r.raise_for_status()
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", r.text, re.S):
        m = re.search(r'data-pbancNo="(\d+)"[^>]*data-pbancKndCd="(\d+)"[^>]*data-bizTyNm="([^"]*)"', row, re.S)
        if not m:
            continue
        pno, knd, biz = m.groups()
        if not any(k in biz for k in types):
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


FAIL_ALERT_AFTER = 3  # 소스가 이 횟수만큼 연속 실패하면 텔레그램으로 알린다


def _run_source(name: str, fn, *args) -> list[dict]:
    """소스 하나를 실행하고 연속 실패 횟수를 DB에 기록.
    조용히 넘어가면 공고를 놓친 걸 모르므로 N회 연속 실패 시 텔레그램 경고를 보낸다."""
    from app import db, notify

    key = f"fail_streak:{name}"
    try:
        got = fn(*args)
        log.info("%s: %d건", name, len(got))
        if (db.get_meta(key) or "0") != "0":
            db.set_meta(key, "0")
            notify.send_telegram(f"✅ 수집 복구: {name} 정상화됐습니다.")
        return got
    except Exception as e:  # noqa: BLE001
        streak = int(db.get_meta(key) or "0") + 1
        db.set_meta(key, str(streak))
        log.error("%s 실패(%d회 연속): %s", name, streak, e)
        if streak == FAIL_ALERT_AFTER:
            notify.send_telegram(
                f"⚠️ 수집 실패 경고\n{name}이(가) {streak}회 연속 실패했습니다.\n"
                f"마지막 오류: {e}\n공고를 놓치고 있을 수 있습니다."
            )
        return []


def collect_all() -> list[dict]:
    """활성화된 소스 전부에서 수집. 실패한 소스는 건너뛰고 연속 실패 시 알린다."""
    from app.boards import fetch_boards

    if os.environ.get("MOCK") == "1":
        log.info("MOCK 모드: 샘플 데이터 사용")
        return fetch_mock() + fetch_boards()

    from app.rules import load_config
    cfg = load_config()
    rent_types = cfg.get("lh_gh_rent_types") or list(RENT_TYPES)

    notices: list[dict] = []
    ah_key = os.environ.get("APPLYHOME_API_KEY", "").strip()
    lh_key = os.environ.get("LH_API_KEY", "").strip()

    if ah_key:
        for fn in (fetch_applyhome, fetch_applyhome_remndr, fetch_pbl_pvt_rent):
            notices += _run_source(fn.__name__, fn, ah_key)
    else:
        log.warning("APPLYHOME_API_KEY 미설정 — 청약홈 수집 건너뜀")

    if lh_key:
        notices += _run_source("fetch_lh", fetch_lh, lh_key)                     # 분양(05+39)
        notices += _run_source("fetch_lh_lease", fetch_lh_lease, lh_key, rent_types)  # 임대(06+39)
    else:
        log.warning("LH_API_KEY 미설정 — LH 수집 건너뜀")

    # GH 경기주택도시공사 (키 불필요, 목록 크롤)
    notices += _run_source("fetch_gh", fetch_gh, rent_types)

    notices += fetch_boards(cfg.get("boards"))
    return notices
