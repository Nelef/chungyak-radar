"""분양가 + 계약금/중도금/잔금 조건 → 당첨 시 '필요 현금' 계산.

homedubu.com 방식(시점별 자금계획)을 따른다:
  - 계약 시점 최소현금 = 계약금(공고 %) + 중도금 자납분(대출 불가 회차)
  - 입주 총 자기자본  = 분양가 − 주담대한도(LTV × 분양가)

데이터 출처:
  - 분양가: 청약홈 주택형별 API(getAPTLttotPblancMdl) LTTOT_TOP_AMOUNT(만원)
  - 계약금율·중도금 자납비율·잔금율: 공고문 PDF를 LLM(Kimi)이 추출
  - LTV: 규제지역 여부(rules가 태깅) → config의 규제/비규제 값

LLM 미설정이면 공고문 추출을 건너뛰고 계약금 가정치(config)로 폴백한다.
공고문이 PDF가 아니면(HWP 등) 조용히 건너뛴다. 결과는 공고ID별 DB 캐시.
"""
import base64
import io
import json
import logging
import os
import re

import requests

from app import db

log = logging.getLogger("cost")


class LLMDownError(Exception):
    """LLM 서버에 도달하지 못함(연결 실패·타임아웃·HTTP 오류).
    '분석했으나 정보 없음'과 달리 실패를 캐시하지 않고, 대시보드에 '서버 죽음'으로 안내한다."""

APPLYHOME_MDL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getAPTLttotPblancMdl"
REMNDR_MDL = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/getRemndrLttotPblancMdl"
DETAIL_URL = "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancDetail.do"
REMNDR_DETAIL = "https://www.applyhome.co.kr/ai/aia/selectAPTRemndrLttotPblancDetailView.do"
LH_LIST = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancList.do"
LH_INFO = "https://apply.lh.or.kr/lhapply/apply/wt/wrtanc/selectWrtancInfo.do"
LH_FILE = "https://apply.lh.or.kr/lhapply/lhFile.do"
HEADERS = {"User-Agent": "Mozilla/5.0 (chungyak-radar; personal use)"}

DEFAULTS = {
    "enabled": True,
    "ltv_regulated": 40,           # 규제지역 주담대 LTV %
    "ltv_normal": 70,              # 비규제 주담대 LTV %
    "loan_cap_manwon": 0,          # 일반 분양 대출 절대한도(만원). 0=LTV만
    "hopetown_ltv": 70,            # 신혼희망타운 수익공유형 모기지 LTV %
    "hopetown_loan_cap_manwon": 40000,  # 신희타 모기지 정액 한도(4억)
    "min_area_m2": 49,             # 전용 이 면적 이상 주택형만 계산(전부 소형이면 전체)
    "highlight_under_manwon": 20000,    # 필요현금이 이 값 미만이면 🔥 강조(2억)
    "fallback_contract_pct": 10,   # 공고문 추출 실패 시 계약금 가정 %
}

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_cache (
    notice_id  TEXT PRIMARY KEY,
    result     TEXT,          -- JSON(성공) 또는 NULL(실패)
    ok         INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    history    TEXT           -- 최근 성공 결과 3개 [{at,r}]
);
"""

def init():
    with db.conn() as c:
        c.executescript(CACHE_SCHEMA)
        try:  # 기존 DB에 history 컬럼 없으면 추가
            c.execute("ALTER TABLE cost_cache ADD COLUMN history TEXT")
        except Exception:  # noqa: BLE001
            pass


def _cache_get(nid):
    with db.conn() as c:
        row = c.execute("SELECT * FROM cost_cache WHERE notice_id = ?", (nid,)).fetchone()
    if not row:
        return "miss"
    return json.loads(row["result"]) if row["ok"] and row["result"] else None


def _cache_put(nid, result, ok):
    with db.conn() as c:
        row = c.execute("SELECT history FROM cost_cache WHERE notice_id = ?", (nid,)).fetchone()
        hist = json.loads(row["history"]) if row and row["history"] else []
        if result:  # 성공 결과만 최근 3개 히스토리에 기록
            hist = ([{"at": db.now_kst(), "r": result}] + hist)[:3]
        c.execute(
            """INSERT INTO cost_cache (notice_id, result, ok, checked_at, history) VALUES (?,?,?,?,?)
               ON CONFLICT(notice_id) DO UPDATE SET result=excluded.result,
                 ok=excluded.ok, checked_at=excluded.checked_at, history=excluded.history""",
            (nid, json.dumps(result, ensure_ascii=False) if result else None,
             1 if ok else 0, db.now_kst(), json.dumps(hist, ensure_ascii=False)),
        )


# ---------------------------------------------------------------- 분양가
def cache_history(nid):
    """공고 최근 분석 히스토리(최대 3개). 대시보드 표시용."""
    with db.conn() as c:
        row = c.execute("SELECT history FROM cost_cache WHERE notice_id = ?", (nid,)).fetchone()
    return json.loads(row["history"]) if row and row["history"] else []


def _fetch_prices(house_manage_no, service_key, url=APPLYHOME_MDL):
    """주택형별 분양가(만원) 목록. url=REMNDR_MDL이면 무순위/잔여세대."""
    params = {
        "page": 1, "perPage": 100, "serviceKey": service_key,
        "cond[HOUSE_MANAGE_NO::EQ]": house_manage_no,
    }
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for it in r.json().get("data", []):
        try:
            price = int(str(it.get("LTTOT_TOP_AMOUNT")).replace(",", "").strip())
        except (TypeError, ValueError):
            continue
        if price > 0:
            out.append({
                "ty": (it.get("HOUSE_TY") or "").strip(), "price": price,
                "supply": _int(it.get("SUPLY_HSHLDCO")),      # 일반공급 세대수
                "sp_supply": _int(it.get("SPSPLY_HSHLDCO")),  # 특별공급 세대수
            })
    return out


# ---------------------------------------------------------------- 공고문 PDF
def _pdf_from_detail_html(html):
    """상세페이지 HTML에서 getAtchmnfl.do 첨부 중 '공고문' PDF bytes. 없으면 None."""
    urls = re.findall(r'https://static\.applyhome\.co\.kr/[^"\']*getAtchmnfl\.do[^"\']*', html)
    best = None
    for u in urls[:6]:
        try:
            fr = requests.get(u.replace("&amp;", "&"), headers=HEADERS, timeout=60)
            fr.raise_for_status()
        except Exception:  # noqa: BLE001
            continue
        data = fr.content
        if data[:4] != b"%PDF":
            continue  # HWP 등 비PDF 첨부는 제외
        if "공고문" in requests.utils.unquote(fr.headers.get("Content-Disposition", "")):
            return data
        best = best or data
    return best


def _fetch_gonggo_pdf(house_manage_no, pblanc_no, detail_url=DETAIL_URL):
    """상세페이지(hmn/pbn 파라미터) → 공고문 PDF bytes. 없으면 None."""
    r = requests.get(detail_url, params={"houseManageNo": house_manage_no, "pblancNo": pblanc_no},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    return _pdf_from_detail_html(r.text)


def _fetch_gonggo_pdf_url(full_url):
    """PBLANC_URL(houseSecd 등 포함) 전체를 그대로 열어 공고문 PDF bytes. 없으면 None."""
    r = requests.get(full_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return _pdf_from_detail_html(r.text)


def _fetch_lh_pdf(notice):
    """청약홈에 첨부가 없는 LH 공고 → LH 사이트에서 공고명으로 검색해 공고문 PDF를 받는다.
    목록(data-id1~4=panId·연계·상위·유형) → 상세(fileDownLoad) → lhFile.do 다운로드."""
    name = notice.get("name") or ""
    key = re.split(r"[(（]", name)[0].strip()  # 첫 괄호 앞 (예: '역곡지구 하우스토리')
    if len(key) < 3:
        return None
    r = requests.get(LH_LIST, params={"mi": 1027, "srchWord": key}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", key)
    best, best_score = None, 0
    for m in re.finditer(
        r'data-id1="(\d+)"\s+data-id2="([^"]*)"\s+data-id3="([^"]*)"\s+data-id4="([^"]*)"'
        r'\s+class="wrtancInfoBtn"[^>]*>(.*?)</a>', r.text, re.S):
        txt = re.sub(r"<[^>]+>", "", m.group(5))
        score = sum(1 for t in tokens if t in txt)
        if score > best_score:
            best, best_score = m.groups()[:4], score
    if not best:
        return None
    pan, c2, c3, c4 = best
    ir = requests.get(LH_INFO, params={"panId": pan, "ccrCnntSysDsCd": c2, "uppAisTpCd": c3,
                                       "aisTpCd": c4, "mi": 1027}, headers=HEADERS, timeout=30)
    ir.raise_for_status()
    fid = None
    for fm in re.finditer(r"fileDownLoad\('(\d+)'\)[^>]*>(.*?)</a>", ir.text, re.S):
        fnm = re.sub(r"<[^>]+>", "", fm.group(2))
        if "공고문" in fnm and ".pdf" in fnm.lower():
            fid = fm.group(1)
            break
    if not fid:
        return None
    fr = requests.get(LH_FILE, params={"fileid": fid}, headers=HEADERS, timeout=60)
    fr.raise_for_status()
    return fr.content if fr.content[:4] == b"%PDF" else None


# -------------------------------------------- OpenAI 호환 LLM (gemma4 등, 텍스트)
OPENAI_PROMPT = (
    "아래 아파트 입주자모집공고문 텍스트에서 자금 조건을 추출해 JSON 객체 하나만 출력하라. "
    "설명·마크다운 없이 JSON만. 키: contract_pct(계약금 %), interim_total_pct(중도금 총 %), "
    "interim_self_pay_pct(중도금 중 대출불가로 현금 자납해야 하는 % — 전액 대출 가능하면 0), "
    "balance_pct(잔금 %), interest_deferred(중도금 이자후불제면 true), note(옵션/특이사항 한 줄)."
)


def _pdf_text(pdf_bytes, max_chars=40000, keywords=None):
    """공고문 PDF → 텍스트. 관련 키워드가 있는 페이지를 우선 추출."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for pg in reader.pages:
        try:
            pages.append(pg.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    kw = keywords or ("계약금", "중도금", "잔금", "납부", "공급금액", "대출", "융자")
    order = sorted(range(len(pages)), key=lambda i: (-sum(k in pages[i] for k in kw), i))
    out, total = [], 0
    for i in order:
        t = pages[i].strip()
        if not t:
            continue
        out.append(t)
        total += len(t)
        if total >= max_chars:
            break
    return "\n\n".join(out)[:max_chars]


def _parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else None


def _openai_headers(s):
    """OpenAI 호환 요청 공통 헤더/바디 조각.
    temperature를 명시하면(예: Kimi Code — 1만 허용) 그 값을 쓰고 vLLM 전용
    chat_template_kwargs는 생략한다(외부 엔드포인트가 모르는 파라미터라 거절될 수 있음)."""
    headers = {"Content-Type": "application/json"}
    if s.get("user"):
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{s['user']}:{s.get('pass', '')}".encode()).decode()
    elif s.get("api_key"):
        headers["Authorization"] = "Bearer " + s["api_key"]
    return headers


def _openai_body(s, messages, max_tokens):
    body = {
        "model": s.get("model") or "k3",
        "messages": messages, "max_tokens": max_tokens,
        "temperature": float(s["temperature"]) if s.get("temperature") else 0,
    }
    if not s.get("temperature"):
        # gemma4 등 로컬 vLLM: 사고를 꺼서 JSON을 바로 출력하게(사고가 토큰을 잡아먹지 않게)
        body["chat_template_kwargs"] = {"enable_thinking": False}
    else:
        # Kimi k3: reasoning effort를 낮춰 응답 시간을 시놀로지 프록시 60s 제한 안으로
        # (실측: high 37s → low 18s, 공고문 추출 품질은 유지)
        body["reasoning_effort"] = "low"
    return body


def _emit(on_event, **ev):
    """진행 이벤트를 호출자 콜백으로 전달(대시보드 SSE 실시간 표시용)."""
    if on_event:
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001
            pass


def _openai_stream(url, body, headers, on_event):
    """stream=True로 호출해 사고(reasoning_content)/출력 델타를 on_event로 흘리며
    최종 content를 모아 반환한다. SSE라 프록시 60s 제한도 델타가 흘러 우회된다."""
    body = {**body, "stream": True}
    parts = []
    try:
        with requests.post(url, json=body, headers=headers, stream=True, timeout=280) as r:
            r.raise_for_status()
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                think = delta.get("reasoning_content")
                if think:
                    _emit(on_event, thinking=think)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    _emit(on_event, content=text)
    except requests.exceptions.RequestException as e:
        raise LLMDownError(str(e)) from e
    return "".join(parts)


def _openai_terms(pdf_bytes, s, prompt=None, keywords=None, on_event=None):
    """OpenAI 호환 챗 API(Kimi·gemma4 등)로 조건 추출. PDF는 텍스트로 변환해 전송.
    on_event가 있으면 진행 상황/사고/출력을 실시간으로 흘려보낸다(SSE 스트리밍)."""
    _emit(on_event, phase="📄 PDF 텍스트 추출 중…")
    text = _pdf_text(pdf_bytes, keywords=keywords)
    if not text:
        return None
    _emit(on_event, pdf_text=text)  # 추가질문 컨텍스트 캐시용(enrich이 가로채 저장, 브라우저엔 안 보냄)
    body = _openai_body(s, [{"role": "user", "content": (prompt or OPENAI_PROMPT) + "\n\n---공고문---\n" + text}], 8000)
    url = s["base_url"].rstrip("/") + "/v1/chat/completions"
    if on_event:
        _emit(on_event, phase="🤖 LLM 분석 중…")
        return _parse_json(_openai_stream(url, body, _openai_headers(s), on_event))
    try:
        r = requests.post(url, json=body, headers=_openai_headers(s), timeout=180)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise LLMDownError(str(e)) from e
    return _parse_json(r.json()["choices"][0]["message"]["content"])


# ------------------------------------------------------ LLM 설정 (DB 영속·웹 편집)
_LLM_FIELDS = ("provider", "base_url", "model", "user", "pass", "api_key", "temperature")


def llm_settings():
    """DB(meta)에 저장된 LLM 설정. provider가 비어 있으면 LLM 미사용."""
    s = {k: (db.get_meta("llm_" + k) or "").strip() for k in _LLM_FIELDS}
    return s


def save_llm_settings(d):
    """대시보드에서 넘어온 설정 저장. 비번/키는 빈값이면 기존 유지."""
    for k in ("provider", "base_url", "model", "user", "temperature"):
        if d.get(k) is not None:
            db.set_meta("llm_" + k, str(d[k]).strip())
    for k in ("pass", "api_key"):
        v = (d.get(k) or "").strip()
        if v and v != "********":
            db.set_meta("llm_" + k, v)


def _llm_terms(pdf_bytes, s, prompt=None, keywords=None, on_event=None):
    prov = (s.get("provider") or "").lower()
    if prov in ("openai", "custom", "vllm"):
        return _openai_terms(pdf_bytes, s, prompt, keywords, on_event)
    return None


def _llm_json(prompt, s, on_event=None):
    """PDF 없이 텍스트 프롬프트만으로 LLM JSON 응답(예: 메타데이터 순위조언)."""
    prov = (s.get("provider") or "").lower()
    if prov in ("openai", "custom", "vllm"):
        body = _openai_body(s, [{"role": "user", "content": prompt}], 2000)
        url = s["base_url"].rstrip("/") + "/v1/chat/completions"
        if on_event:
            _emit(on_event, phase="🤖 순위 조언 생성 중…")
            return _parse_json(_openai_stream(url, body, _openai_headers(s), on_event))
        try:
            r = requests.post(url, json=body, headers=_openai_headers(s), timeout=120)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise LLMDownError(str(e)) from e
        return _parse_json(r.json()["choices"][0]["message"]["content"])
    return None


def _meta_rank_prompt(notice, price_lo_manwon, profile, remndr=False):
    if remndr:
        ask = ("이 공고는 무순위/잔여세대(줍줍)이다. 다음 4가지를 반드시 명시하라: "
               "①청약통장 사용 안 함 → 당첨·포기해도 통장 그대로 유지 ②재당첨 제한 적용 여부"
               "(규제지역·분양가상한제면 당첨 시 제한) ③실거주 의무·거주지역/무주택 요건 "
               "④주택 소유 간주 시점: 당첨만으로는 소유 아님(계약 미체결·포기 시 무주택 유지), "
               "계약 체결 시 분양권 취득으로 유주택 간주. '당첨=소유'라고 하지 말 것. 2~3문장.")
    else:
        ask = ("공고명에 드러난 유형(민영/공공분양/신혼희망타운 등)과 신청자 상황을 바탕으로, "
               "신청 가능한 특별공급 유형과 일반공급 순위, 소득기준 충족 여부를 2~3문장으로 조언하라.")
    return (
        f"청약 공고명: '{notice.get('name')}' (최저 분양가 약 {int(price_lo_manwon)}만원). "
        f"신청자 상황: {_who(profile)}. {ask} "
        'JSON 객체 {"rank_advice":"..."} 하나만 출력.'
    )


# ------------------------------------------------ 임대(청년안심) 공고문 추출
SOCO_FILE = "https://soco.seoul.go.kr/coHouse/cmmn/file/fileDown.do?atchFileId={aid}&fileSn=1"
RENT_KEYWORDS = ("신청", "접수", "청약", "보증금", "임대료", "임대조건", "모집", "일정",
                 "소득", "자격", "순위", "거주")


# 2025년 도시근로자 가구원수별 가구당 월평균소득(원/월) — 공고문에 기준표가 없을 때의 표준 판정표.
# 서울주거포털(seoulhousing.kr)·청년안심 공고문 교차검증(2026-07). 갱신은 config.yaml income_standard_2025.
INCOME_STANDARD_TEXT = (
    "표준 소득기준(2025년 도시근로자 월평균소득, 원/월): "
    "1인 100% 3,813,363 / 110% 4,194,699 / 120% 4,576,036, "
    "2인 100% 5,866,270 / 110% 6,452,897 / 120% 7,039,524, "
    "3인 100% 8,168,429 / 120% 9,802,115. "
    "공고문에 소득기준표가 없으면 이 표준표로 판정하라."
)


def _rent_prompt(profile=None):
    return (
        "아래 임대주택(청년안심·SH 매입임대·공공임대 등) 입주자모집공고문에서 JSON 객체 하나만 출력하라. "
        "설명·마크다운 없이 JSON만. "
        "키: apply_start(청약/신청 접수 시작일 YYYY-MM-DD), apply_end(접수 마감일 YYYY-MM-DD), "
        "deposit_manwon(임대보증금=계약 시 목돈으로 내는 큰 금액, 만원 단위 숫자, 여러 타입이면 최저), "
        "monthly_manwon(월 임대료=매달 내는 작은 금액, 만원 단위, 없으면 0), "
        "area_m2(전용면적 최소 ㎡), "
        "move_in(입주 가능일/입주예정일 YYYY-MM-DD, 없으면 공고문 표기 그대로, 없으면 ''), "
        "lease_term(임대(거주) 계약기간 문자열, 예 '2년'·'최장 6년', 없으면 ''), "
        "contract_pct(계약금 비율 % — 보증금 대비, 공고문에 명시 없으면 10), "
        "income_limit(공고문의 소득기준을 한 줄 요약 — 예 '1인 100% 381만/110% 419만/120% 458만'. "
        "공고문에 소득기준이 아예 없으면 '기준 없음(표준 100/110/120% 적용)'이라고 표기), "
        "residency_req(거주지·지역 순위 기준 한 줄 — 예 '영등포구 1순위, 서울 2순위, 그 외 3순위'. 없으면 ''), "
        f"rank_advice(신청자 상황: {_who(profile)}. {INCOME_STANDARD_TEXT} "
        "신청자의 실제 월소득과 비교해 청년 1인 가구 각자 기준·신혼부부 2인 합산 기준으로 "
        "신청 가능한 순위(1/2/3순위) 또는 불가를 구체 숫자와 함께 한국어 2~3문장으로 판정), "
        "note(특이사항 한 줄). 보증금과 월임대료를 혼동하지 말 것."
    )


def _rent_llm_merge(t, pdf, profile, on_event=None):
    """임대: 신청기간·입주가능일·거주기간·계약금율·자격조언 같은 정성 필드를 LLM으로 채운다.
    보증금/월세/전용은 pdfplumber 표 파싱이 우선이라 이미 있으면 덮지 않는다(not t.get)."""
    s = llm_settings()
    if not (s.get("provider") and pdf):
        return
    try:
        lt = _llm_terms(pdf, s, _rent_prompt(profile), RENT_KEYWORDS, on_event) or {}
    except LLMDownError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("임대 LLM 보강 실패: %s", e)
        return
    for k in ("apply_start", "apply_end", "deposit_manwon", "monthly_manwon", "area_m2",
              "move_in", "lease_term", "contract_pct", "income_limit", "residency_req",
              "rank_advice", "note"):
        if lt.get(k) and not t.get(k):
            t[k] = lt[k]


def _rent_price(pdf_bytes):
    """pdfplumber로 임대조건 표를 파싱 → 최저 보증금 옵션의 보증금·월임대료·전용면적(만원/㎡).
    공고별 편차가 커서 헤더의 '보증금'/'임대료' 컬럼 위치를 찾아 컬럼 기준으로 읽는다:
    - 보증금 30%/50%/70%처럼 한 행에 여러 옵션 → 최저 보증금(entry) tier를 고름
    - 임대료가 소수점(40.2)일 수 있음 / 단위가 만원(6,000) or 원(79,380,000)일 수 있음(자동 환산)."""
    import pdfplumber

    def num(c):
        c = (c or "").replace("\n", "").replace(",", "").strip()
        return float(c) if re.fullmatch(r"\d+(?:\.\d+)?", c) else None

    best = None  # (deposit_manwon, monthly_manwon, area_m2)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        for page in doc.pages:
            for table in (page.extract_tables() or []):
                flat = " ".join(str(c) for row in table for c in (row or []) if c)
                if "보증금" not in flat or "임대료" not in flat:
                    continue
                # 헤더에서 보증금/임대료 컬럼 인덱스 파악.
                # '보증금30%'·'임대보증금'·'월임대료' 등 표기 편차를 포함검색으로 잡고,
                # '기준임대료'(보증금·임대료를 묶는 그룹헤더)가 보증금 컬럼과 겹치면 임대료에서 뺀다.
                dep_cols, rent_cols = set(), set()
                for row in table:
                    for i, c in enumerate(row):
                        t = (c or "").replace("\n", "").replace(" ", "")
                        if "보증금" in t:
                            dep_cols.add(i)
                        if "임대료" in t:
                            rent_cols.add(i)
                rent_cols -= dep_cols
                dep_cols, rent_cols = sorted(dep_cols), sorted(rent_cols)
                if not dep_cols or not rent_cols:
                    continue
                for row in table:
                    deps = [(k, num(row[i])) for k, i in enumerate(dep_cols)
                            if i < len(row) and num(row[i]) and num(row[i]) >= 1000]
                    if not deps:
                        continue
                    k, dep = min(deps, key=lambda t: t[1])  # 최저 보증금 옵션
                    rent = num(row[rent_cols[k]]) if k < len(rent_cols) and rent_cols[k] < len(row) else None
                    area = None
                    for c in row:
                        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m2|㎡|m²)", (c or "").replace("\n", ""))
                        if m:
                            area = float(m.group(1)); break
                    if dep >= 1_000_000:  # 원 단위 표 → 만원 환산
                        dep = dep / 10000
                        rent = rent / 10000 if rent and rent >= 10000 else rent
                    if not (500 <= dep <= 200000):  # 5백만~20억 벗어나면 오검출 → 버림
                        continue
                    entry = (int(round(dep)), int(round(rent)) if rent else 0, area)
                    if best is None or entry[0] < best[0]:
                        best = entry
    if not best:
        return None
    return {"deposit_manwon": best[0], "monthly_manwon": best[1], "area_m2": best[2]}


def _build_rent(notice, min_area=0, profile=None, on_event=None):
    aid = (notice.get("raw") or {}).get("atchFileId")
    if not aid:
        return None
    _emit(on_event, phase="📄 공고문 다운로드 중…")
    fr = requests.get(SOCO_FILE.format(aid=aid), headers=HEADERS, timeout=60)
    fr.raise_for_status()
    if fr.content[:4] != b"%PDF":
        return None
    pdf = fr.content
    s = llm_settings()
    t = {}
    if s.get("provider"):  # 신청기간·입주·거주기간·계약금·자격조언은 LLM으로
        t = _llm_terms(pdf, s, _rent_prompt(profile), RENT_KEYWORDS, on_event) or {}
    try:  # 보증금/월세/전용은 pdfplumber 표 파싱으로 덮어씀(결정론적·정확)
        price = _rent_price(pdf)
        if price:
            t.update(price)
        if min_area:
            t["units"] = _rent_units(pdf, min_area)
    except Exception as e:  # noqa: BLE001
        log.warning("임대료 표 파싱 실패(%s): %s", notice.get("name"), e)
    if not t.get("apply_start"):
        t["apply_start"] = notice.get("r1_start")  # 목록 optn4(신청시작) 폴백
    if not (t.get("apply_start") or t.get("deposit_manwon")):
        return None
    t["kind"] = "rent"
    return t


def _fetch_gh_pdf(notice):
    """GH 상세페이지 첨부(selectFileDown.do) 중 공고문 PDF bytes. 카탈로그보다 '공고문' 우선."""
    from app.sources import GH_DETAIL, UA, gh_session

    raw = notice.get("raw") or {}
    pno = raw.get("pbancNo") or notice["id"].split(":")[-1]
    s = gh_session()
    r = s.get(GH_DETAIL, params={"previewYn": "N", "pbancNo": pno,
                                 "pbancKndCd": raw.get("pbancKndCd") or "01",
                                 "bizTyNm": raw.get("bizTyNm") or ""},
              headers=UA, timeout=25, verify=False)
    r.raise_for_status()
    # <a href="/sr/sr7150/selectFileDown.do?...">파일명.pdf</a> — 파일명으로 공고문 우선순위
    links = re.findall(r'href="(/sr/sr7150/selectFileDown\.do\?[^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    cands = []
    for href, label in links:
        nm = re.sub(r"<[^>]+>", "", label).strip()
        if ".pdf" not in nm.lower():
            continue
        cands.append((0 if "공고문" in nm else 1, href))
    for _, href in sorted(cands)[:4]:
        try:
            fr = s.get("https://apply.gh.or.kr" + href.replace("&amp;", "&"),
                       headers=UA, timeout=60, verify=False)
            if fr.content[:4] == b"%PDF":
                return fr.content
        except Exception:  # noqa: BLE001
            continue
    return None


def _fetch_lh_rent_pdf(notice):
    """LH 임대 공고문 PDF — 분양과 동일한 LH 사이트 크롤(_fetch_lh_pdf) 재사용."""
    n = dict(notice)
    n["name"] = re.sub(r"^\[[^\]]*\]\s*", "", n.get("name") or "")  # '[행복주택] ' 접두 제거
    return _fetch_lh_pdf(n)


def _build_public_rent(notice, min_area=0, profile=None, on_event=None):
    """LH임대·GH 공고 — 접수기간은 목록에 있고, 공고문 PDF에서 보증금/월세/평형을 뽑는다."""
    t = {"kind": "rent", "apply_start": notice.get("r1_start"), "apply_end": notice.get("r1_end")}
    src = notice.get("source")
    _emit(on_event, phase="📄 공고문 다운로드 중…")
    try:
        pdf = _fetch_gh_pdf(notice) if src == "gh" else _fetch_lh_rent_pdf(notice)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 공고문 PDF 실패(%s): %s", src, notice.get("name"), e)
        pdf = None
    if pdf:
        try:  # 보증금/월세/전용은 표 파싱(결정론적) 우선
            price = _rent_price(pdf)
            if price:
                t.update(price)
            if min_area:
                t["units"] = _rent_units(pdf, min_area)
        except Exception as e:  # noqa: BLE001
            log.warning("임대료 표 파싱 실패(%s): %s", notice.get("name"), e)
        _rent_llm_merge(t, pdf, profile, on_event)  # 입주·거주기간·계약금·자격조언 + 표 실패 시 보증금 폴백
    if not t.get("area_m2"):  # GH 상세의 전용면적 범위(예: '26.9294 ~ 51.9289') 최저값
        m = re.search(r"(\d+(?:\.\d+)?)", (notice.get("raw") or {}).get("area_text") or "")
        if m:
            t["area_m2"] = round(float(m.group(1)), 1)
    return t


def _build_applyhome_rent(notice, min_area=0, profile=None, on_event=None):
    """공공지원 민간임대(청약홈) — 접수기간은 API에 이미 있으니 공고문 PDF로 보증금/월세만 뽑는다."""
    url = notice.get("url")
    t = {"kind": "rent", "apply_start": notice.get("r1_start"), "apply_end": notice.get("r1_end")}
    _emit(on_event, phase="📄 공고문 다운로드 중…")
    pdf = _fetch_gonggo_pdf_url(url) if url else None
    if pdf:
        try:  # 표 파싱(결정론적) 우선
            price = _rent_price(pdf)
            if price:
                t.update(price)
            if min_area:
                t["units"] = _rent_units(pdf, min_area)
        except Exception as e:  # noqa: BLE001
            log.warning("임대료 표 파싱 실패(%s): %s", notice.get("name"), e)
        _rent_llm_merge(t, pdf, profile, on_event)  # 입주·거주기간·계약금·자격조언 + 표 실패 시 보증금 폴백
    return t


# ---------------------------------------------------------------- 계산·표기
def _man(v):
    """만원 정수 → '7,830만' / '2억 3,450만' 표기."""
    v = int(round(v))
    eok, man = divmod(v, 10000)
    if eok and man:
        return f"{eok}억 {man:,}만"
    if eok:
        return f"{eok}억"
    return f"{man:,}만"


def _num(v):
    """10 또는 10.0 → '10' 표기."""
    return f"{float(v):g}"


def _area(ty):
    """주택형 문자열('084.9796A') → 전용면적 84.98."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)", ty or "")
    return float(m.group(1)) if m else 0.0


def _int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _rent_units(pdf_bytes, min_area):
    """임대 공고문 표에서 전용 min_area㎡ 이상 평형별 (전용·최저보증금·월세·공급호수) 목록.
    보증금/임대료/공급호수 컬럼을 헤더로 찾아 읽고, 같은 면적은 최저 보증금 옵션으로 합친다."""
    import pdfplumber

    def num(c):
        c = (c or "").replace("\n", "").replace(",", "").strip()
        return float(c) if re.fullmatch(r"\d+(?:\.\d+)?", c) else None

    merged = {}  # area(반올림) -> (deposit, monthly, supply)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as doc:
        for page in doc.pages:
            for table in (page.extract_tables() or []):
                flat = " ".join(str(c) for row in table for c in (row or []) if c)
                if "보증금" not in flat or "임대료" not in flat:
                    continue
                dep_cols, rent_cols, sup_cols = set(), set(), set()
                for row in table:
                    for i, c in enumerate(row):
                        t = (c or "").replace("\n", "").replace(" ", "")
                        if "보증금" in t:
                            dep_cols.add(i)
                        if "임대료" in t:
                            rent_cols.add(i)
                        if "공급호수" in t or "공급세대" in t:
                            sup_cols.add(i)
                rent_cols -= dep_cols
                dep_cols, rent_cols = sorted(dep_cols), sorted(rent_cols)
                sup_col = min(sup_cols) if sup_cols else None
                if not dep_cols or not rent_cols:
                    continue
                for row in table:
                    area = None
                    for c in row:
                        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:m2|㎡|m²)", (c or "").replace("\n", ""))
                        if m:
                            area = float(m.group(1)); break
                    if not area or area < min_area:
                        continue
                    deps = [(k, num(row[i])) for k, i in enumerate(dep_cols)
                            if i < len(row) and num(row[i]) and num(row[i]) >= 1000]
                    if not deps:
                        continue
                    k, dep = min(deps, key=lambda x: x[1])
                    rent = num(row[rent_cols[k]]) if k < len(rent_cols) and rent_cols[k] < len(row) else None
                    if dep >= 1_000_000:
                        dep = dep / 10000
                        rent = rent / 10000 if rent and rent >= 10000 else rent
                    if not (500 <= dep <= 200000):
                        continue
                    sv = num(row[sup_col]) if (sup_col is not None and sup_col < len(row)) else None
                    sup = int(sv) if sv else None
                    a = round(area, 1)
                    d, r = int(round(dep)), (int(round(rent)) if rent else 0)
                    if a in merged:  # 같은 평형이 특공·일반 표에 나뉘어 있으면 세대수 합산, 보증금은 최저
                        pd_, pr_, ps_ = merged[a]
                        tot = None if (ps_ is None and sup is None) else (ps_ or 0) + (sup or 0)
                        merged[a] = (d, r, tot) if d < pd_ else (pd_, pr_, tot)
                    else:
                        merged[a] = (d, r, sup)
    return [{"area": a, "deposit": v[0], "monthly": v[1], "supply_total": v[2]}
            for a, v in sorted(merged.items())]


SALE_KEYWORDS = ("계약금", "중도금", "잔금", "납부", "공급금액", "대출", "융자",
                 "소득", "자격", "특별공급", "공급대상", "순위", "신혼", "생애최초")


def _who(profile):
    who = (profile or {}).get("household") or "무주택 세대"
    inc = (profile or {}).get("annual_income_manwon")
    s = who + (f", 부부합산 연소득 약 {inc}만원" if inc else "")
    iw = (profile or {}).get("income_2025_won") or {}
    if iw.get("me"):
        me = iw["me"]
        sp = iw.get("spouse") or 0
        s += (f". 2025년 소득: 본인 {me:,}원(월 {me/12/10000:.0f}만), "
              f"배우자 {sp:,}원(월 {sp/12/10000:.0f}만), 합산 월 {(me+sp)/12/10000:.0f}만")
    return s


def _sale_prompt(profile, remndr=False):
    if remndr:
        rank = (f"rank_advice(무순위/잔여세대(줍줍) 공고다. 신청자={_who(profile)}. 다음 4가지를 반드시 명시: "
                f"①청약통장 사용 안 함 → 당첨·포기해도 통장 그대로 유지 ②재당첨 제한 적용 여부"
                f"(공고문상 규제지역·분양가상한제면 당첨 시 제한, 기간까지) ③실거주 의무·거주지역/무주택 요건 "
                f"④주택 소유 간주 시점을 정확히: 당첨만으로는 주택 소유 아님(계약 미체결·포기 시 무주택 유지), "
                f"계약 체결 시 분양권 취득으로 유주택 간주. '당첨=소유'라고 하지 말 것. 한국어 2~3문장)")
    else:
        rank = (f"rank_advice(신청자 상황: {_who(profile)}. 이 공고에서 신청 가능한 특별공급 유형과 "
                "일반공급 순위, 소득기준 충족 여부를 한국어 2~3문장으로 조언)")
    return (
        "아래 아파트 입주자모집공고문에서 JSON 객체 하나만 출력하라. 설명·마크다운 없이 JSON만. "
        "키: contract_pct(계약금 %), interim_total_pct(중도금 총 %), "
        "interim_self_pay_pct(중도금 중 대출불가로 현금 자납해야 하는 % — 전액 대출 가능하면 0), "
        "balance_pct(잔금 %), interest_deferred(중도금 이자후불제면 true), "
        "schedule(납부 일정 배열 — 계약금·중도금 각 회차·잔금을 납부 순서대로. 각 원소는 "
        '{"label":"계약금"|"중도금 1차"|…|"잔금", '
        '"when":공고문의 납부일 "YYYY-MM-DD"(절대일자가 있으면) 또는 "계약일"·"계약후 6개월" 등 공고문 표기 그대로, '
        '"pct":분양가 대비 납부 비율 숫자}. 공고문에 납부일정표가 없으면 빈 배열 []), '
        f"{rank}, note(특이사항 한 줄)."
    )


def _build(notice, regulated, ccfg, profile=None, remndr=False, on_event=None):
    ah_key = os.environ.get("APPLYHOME_API_KEY", "").strip()
    if not ah_key:
        return None
    raw = notice.get("raw") or {}
    hmn = raw.get("HOUSE_MANAGE_NO") or notice["id"].split(":")[-1]
    pbn = raw.get("PBLANC_NO") or hmn

    _emit(on_event, phase="💰 분양가 조회 중…")
    all_prices = _fetch_prices(hmn, ah_key, REMNDR_MDL if remndr else APPLYHOME_MDL)
    if not all_prices:
        return None
    min_area = float(ccfg.get("min_area_m2") or 0)
    big = [p for p in all_prices if _area(p["ty"]) >= min_area] if min_area else all_prices
    prices = big or all_prices  # 요약 필요현금은 ≥49㎡ 최저가, 전부 소형이면(신혼희망타운 등) 전체
    # 무순위(줍줍)는 대부분 소형이라 49㎡ 필터를 적용하면 평형별이 다 사라짐 → 전 타입 표시
    unit_src = all_prices if remndr else big

    lo = min(p["price"] for p in prices)
    hi = max(p["price"] for p in prices)
    areas = [_area(p["ty"]) for p in prices if _area(p["ty"])]
    area_lo = round(min(areas), 1) if areas else None
    area_hi = round(max(areas), 1) if areas else None

    terms, estimated = None, False
    s = llm_settings()
    if s.get("provider"):
        try:
            _emit(on_event, phase="📄 공고문 PDF 다운로드 중…")
            pdf = _fetch_gonggo_pdf(hmn, pbn, REMNDR_DETAIL if remndr else DETAIL_URL)
            if not pdf and not remndr:  # 청약홈에 첨부 없는 LH 공고(신혼희망타운 등)는 LH 사이트에서
                pdf = _fetch_lh_pdf(notice)
            if pdf:
                terms = _llm_terms(pdf, s, _sale_prompt(profile, remndr), SALE_KEYWORDS, on_event)
        except LLMDownError:  # 서버 죽음은 삼키지 말고 위로 전달 → 대시보드가 '서버 죽음' 안내
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 조건 추출 실패(%s): %s", notice.get("name"), e)
    if not terms:  # 공고문 PDF 없음(LH 공공분양 등) → 계약금 가정 + 순위조언은 메타데이터로
        terms = {"contract_pct": ccfg["fallback_contract_pct"], "interim_self_pay_pct": 0}
        estimated = True
        if s.get("provider"):
            try:
                rank = _llm_json(_meta_rank_prompt(notice, lo, profile, remndr), s, on_event)
                if rank and rank.get("rank_advice"):
                    terms["rank_advice"] = rank["rank_advice"]
            except LLMDownError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("순위조언(메타) 실패(%s): %s", notice.get("name"), e)
    # 신혼희망타운은 수익공유형 모기지(LTV70·정액 4억 캡). 그 외는 규제/비규제 LTV.
    hopetown = "신혼희망" in (notice.get("name") or "")
    if hopetown:
        ltv = float(ccfg.get("hopetown_ltv") or 70)
        cap = float(ccfg.get("hopetown_loan_cap_manwon") or 0)
    else:
        ltv = float(ccfg["ltv_regulated"] if regulated else ccfg["ltv_normal"])
        cap = float(ccfg.get("loan_cap_manwon") or 0)
    loan = lo * ltv / 100
    cap_hit = bool(cap and loan > cap)
    if cap_hit:
        loan = cap
    equity = lo - loan                   # 필요현금 = 분양가 − 대출(homedubu 방식)
    c_pct = float(terms.get("contract_pct") or 0)
    self_pct = float(terms.get("interim_self_pay_pct") or 0)
    hl = float(ccfg.get("highlight_under_manwon") or 0)
    # 평형별: 각 필요현금 + 세대수(일반+특공). 분양은 ≥49㎡만, 무순위는 전 타입.
    units = []
    for p in sorted(unit_src, key=lambda x: _area(x["ty"])):
        ln = p["price"] * ltv / 100
        if cap and ln > cap:
            ln = cap
        sup, sp = p.get("supply"), p.get("sp_supply")
        units.append({
            "area": round(_area(p["ty"]), 1),
            "cash": int(round(p["price"] - ln)),
            "supply_general": sup, "supply_special": sp,
            "supply_total": ((sup or 0) + (sp or 0)) or None,
        })
    sched = terms.get("schedule")
    sched = [s for s in sched if isinstance(s, dict)] if isinstance(sched, list) else []
    return {
        "kind": "sale", "units": units,
        "price_lo": lo, "price_hi": hi, "area_lo": area_lo, "area_hi": area_hi,
        "contract_pct": c_pct, "interim_self_pct": self_pct,
        "entry_cash": lo * (c_pct + self_pct) / 100,   # 계약~중도금 최소현금(최저가 기준)
        "loan": loan, "equity": equity, "ltv_pct": ltv, "cap_hit": cap_hit,
        "regulated": bool(regulated), "hopetown": hopetown, "remndr": bool(remndr),
        "highlight": bool(hl and equity < hl), "estimated": estimated,
        "schedule": sched,   # 납부 일정(회차·시기·%) — 프런트에서 최저 분양가로 금액 환산
        "rank_advice": terms.get("rank_advice", ""), "note": terms.get("note", ""),
    }


def enrich(notice, cfg, force=False, on_event=None):
    """공고 1건 온디맨드 LLM 분석(캐시). 분양=필요현금+순위조언 / 임대=신청기간+임대료.
    force=True면 캐시 무시하고 다시 분석. 반환: 통합 dict(kind 포함) 또는 None.
    on_event가 있으면 진행 상황·LLM 사고/출력 델타를 실시간으로 전달한다(SSE)."""
    ccfg = {**DEFAULTS, **((cfg or {}).get("cost") or {})}
    if not ccfg.get("enabled"):
        return None
    nid = notice["id"]
    if not force:
        cached = _cache_get(nid)
        if cached != "miss":
            return cached

    def _intercept(ev):
        """pdf_text 이벤트는 DB에 캐시하고(추가질문 컨텍스트), 나머지만 호출자로 전달."""
        if "pdf_text" in ev:
            try:
                db.set_pdf_text(nid, ev["pdf_text"])
            except Exception as e:  # noqa: BLE001
                log.warning("공고문 텍스트 캐시 실패(%s): %s", nid, e)
            return
        _emit(on_event, **ev)

    result = None
    try:
        src = notice.get("source")
        min_area = float(ccfg.get("min_area_m2") or 0)
        if src in ("applyhome", "applyhome_remndr"):
            flags = notice.get("flags") or {}
            regulated = bool(flags.get("투기과열") or flags.get("조정대상"))
            result = _build(notice, regulated, ccfg, (cfg or {}).get("profile"),
                            remndr=(src == "applyhome_remndr"), on_event=_intercept)
        else:
            profile = (cfg or {}).get("profile")
            if src == "board":
                result = _build_rent(notice, min_area, profile, _intercept)
            elif src == "applyhome_rent":
                result = _build_applyhome_rent(notice, min_area, profile, _intercept)
            elif src in ("lh_rent", "gh"):
                result = _build_public_rent(notice, min_area, profile, _intercept)
    except LLMDownError as e:  # 서버 죽음: 실패로 캐시하지 말고(=재시도 가능) 명시적 오류 반환
        log.warning("%s LLM 서버 응답 없음: %s", notice.get("name"), e)
        return {"error": "llm_down", "detail": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        log.warning("%s LLM 분석 실패: %s", notice.get("name"), e)
    _cache_put(nid, result, ok=bool(result))
    return result



# ---------------------------------------------------------------- 추가질문
def ask(notice, question, cfg, on_event=None):
    """분석된 공고에 대한 추가질문. 공고문 텍스트 캐시 + 이전 분석결과 + 이전 Q&A를 컨텍스트로 쓴다.
    반환: 답변 문자열(DB에 Q&A 기록). LLM 미설정/다운이면 LLMDownError."""
    s = llm_settings()
    if not s.get("provider"):
        raise LLMDownError("LLM 미설정 (대시보드 ⚙에서 설정)")
    question = (question or "").strip()
    if not question:
        return None
    nid = notice["id"]
    prompt = [
        f"청약/임대 공고에 대한 질문응답이다. 공고: '{notice.get('name')}' "
        f"({notice.get('region') or ''} {notice.get('house_kind') or ''}). "
        f"신청자 상황: {_who((cfg or {}).get('profile'))}. "
        "아래 근거 자료를 바탕으로 한국어로 간결하게(2~5문장) 답하라. "
        "근거에 없는 내용은 추측하지 말고 공고문 확인이 필요하다고 답하라.",
    ]
    cached = _cache_get(nid)
    if cached and cached != "miss":
        prompt.append("[이전 AI 분석결과]\n" + json.dumps(cached, ensure_ascii=False)[:3000])
    pdf_text = db.get_pdf_text(nid)
    if pdf_text:
        prompt.append("[공고문 텍스트]\n" + pdf_text)
    history = db.list_qa(nid)
    if history:
        prompt.append("[이전 질문응답]\n" + "\n".join(f"Q: {h['q']}\nA: {h['a']}" for h in history[-5:]))
    prompt.append(f"[질문]\n{question}")
    body = _openai_body(s, [{"role": "user", "content": "\n\n".join(prompt)}], 4000)
    url = s["base_url"].rstrip("/") + "/v1/chat/completions"
    if on_event:
        _emit(on_event, phase="🤖 답변 생성 중…")
        answer = _openai_stream(url, body, _openai_headers(s), on_event)
    else:
        try:
            r = requests.post(url, json=body, headers=_openai_headers(s), timeout=180)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise LLMDownError(str(e)) from e
        answer = r.json()["choices"][0]["message"]["content"]
    answer = (answer or "").strip()
    if answer:
        db.add_qa(nid, question, answer)
    return answer
