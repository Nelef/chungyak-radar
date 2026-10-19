"""청약 레이더 — 수집 스케줄러 + 대시보드 서버.

실행: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import json
import logging
import os
import queue
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

# 이미지에 구운 /srv/.env(키·설정)를 로드해 os.environ에 채운다.
# 키를 이미지 ENV로 노출하지 않아 Container Manager가 환경변수를 물어보지 않는다.
# 파일이 없으면(compose에서 env_file로 주입하는 등) 조용히 무시된다. 셸/도커가
# 이미 설정한 값은 덮어쓰지 않는다(override=False 기본).
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app import commute, cost, db, notify, rules, sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

KST = timezone(timedelta(hours=9))
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def run_collection() -> dict:
    """수집 → 필터/태깅 → 저장 → 신규 공고 알림."""
    raw = sources.collect_all()
    processed = rules.process(raw)
    new_matched, total_matched = 0, 0
    for n in processed:
        is_new = db.upsert_notice(n)
        if not n["matched"]:
            continue
        total_matched += 1
        if is_new:
            new_matched += 1
            key = f"{n['id']}:new"
            if not db.is_muted(n["id"]) and not db.already_sent(key):  # 알림 끈 공고는 발송 제외
                notify.send_telegram(rules.new_notice_message(n))
                db.mark_sent(key)
    # 크롤 피드에서 밀려나 재평가 안 되는 기존 게시판 행 정리(노이즈 키워드 + 노후)
    cfg = rules.load_config()
    stale = db.deactivate_board_noise(cfg.get("board_exclude_keywords", []))
    stale += db.deactivate_stale_boards()
    stale += db.deactivate_rent_types(cfg.get("lh_gh_rent_types", []))
    if stale:
        log.info("게시판 공고 %d건 비활성화(노이즈+노후)", stale)
    db.set_meta("last_sync", db.now_kst())
    db.set_meta("last_sync_counts", f"{len(processed)}건 수집 / {total_matched}건 매칭 / 신규 {new_matched}건")
    log.info("수집 완료: %d건, 매칭 %d건, 신규 %d건", len(processed), total_matched, new_matched)
    _enqueue_backfill()   # 새 공고/미분석 공고를 자동 분석 큐에 적재(직렬 처리)
    return {"collected": len(processed), "matched": total_matched, "new": new_matched}


def run_reminders():
    """접수 시작 D-7/D-1, 마감일 리마인더."""
    cfg = rules.load_config()
    today = datetime.now(KST).date()
    for n in db.list_notices(matched_only=True):
        if n.get("muted"):  # 알림 끈 공고는 리마인더도 제외
            continue
        for suffix, msg in rules.due_reminders(n, today, cfg.get("reminders", [7, 1])):
            key = f"{n['id']}:{suffix}"
            if not db.already_sent(key):
                notify.send_telegram(msg)
                db.mark_sent(key)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    commute.init()
    cost.init()
    cfg = rules.load_config()
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    if not DEMO:  # 데모는 정리된 DB 사본을 보여주기만 한다(수집·알림·자동분석 없음 → API·LLM 비용 0)
        sched.add_job(run_collection, "interval", minutes=cfg["poll_interval_minutes"],
                      next_run_time=datetime.now(KST) + timedelta(seconds=5))
        sched.add_job(run_reminders, "cron", hour=8, minute=30)  # 매일 아침 8:30 리마인더
        threading.Thread(target=_auto_worker, daemon=True).start()  # 자동 분석 직렬 워커
    else:
        log.info("DEMO_MODE — 수집·리마인더·자동분석·텔레그램 비활성")
    sched.start()
    log.info("스케줄러 시작 (수집 주기 %d분, 리마인더 08:30)", cfg["poll_interval_minutes"])
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="청약 레이더", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


# ── 로그인 게이트 ────────────────────────────────────────────────
# 비밀번호(전체 권한) 또는 게스트(읽기 전용) 선택. 게스트 쓰기 차단은 반드시
# 서버에서 강제한다(버튼만 숨기면 API 직접 호출로 우회 가능). 비번은 deploy.env의
# DASHBOARD_PASSWORD. 미설정이면 게이트 비활성(전원 전체 권한 — 하위호환).
_DASH_PW = os.environ.get("DASHBOARD_PASSWORD", "").strip()
# 세션은 DB(sessions 테이블, /data 볼륨)에 저장 → 재배포·재시작에도 로그인 유지.
_LOGIN_FAILS: dict = {}       # ip -> [fail timestamps] (재시작 시 리셋되어도 무해)
_SESSION_TTL = 30 * 86400
_MAX_FAILS, _FAIL_WINDOW = 5, 600   # 무차별 대입 방지: 10분에 5회 실패 시 잠금


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


# 공개 데모(uyai 포트폴리오 /radar/). 개인 데이터를 지운 DB 사본으로 띄우는 별도 컨테이너에서만 켠다.
# 데모에선 누구도 로그인할 수 없고(전원 게스트=읽기 전용), 텔레그램·LLM 자동분석·리마인더가 꺼진다.
DEMO = os.environ.get("DEMO_MODE", "") == "1"


def _authed(request: Request) -> bool:
    if DEMO:                    # 데모는 비번이 있어도 로그인 불가 — 항상 게스트
        return False
    if not _DASH_PW:            # 비번 미설정 = 게이트 없음(전원 허용)
        return True
    return db.session_valid(request.cookies.get("cr_session", ""))


def _forbid():
    return JSONResponse({"error": "forbidden",
                         "detail": "로그인이 필요합니다 (게스트는 읽기 전용)."}, status_code=403)


@app.get("/api/whoami")
def api_whoami(request: Request):
    """현재 세션 권한. 대시보드가 게이트를 띄울지 판단."""
    return {"authed": _authed(request), "gated": bool(_DASH_PW) or DEMO, "demo": DEMO}


@app.post("/api/login")
def api_login(request: Request, payload: dict = Body(...)):
    """비밀번호 로그인. IP별 실패 횟수 제한으로 무차별 대입 차단."""
    if not _DASH_PW:
        return {"ok": True}
    ip = _client_ip(request)
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    if len(fails) >= _MAX_FAILS:
        wait = int(_FAIL_WINDOW - (now - fails[0]))
        _LOGIN_FAILS[ip] = fails
        return JSONResponse({"ok": False, "locked": True, "retry_after": wait,
                             "error": f"시도 횟수 초과 — 약 {wait // 60 + 1}분 후 다시 시도하세요."},
                            status_code=429)
    pw = str((payload or {}).get("password", ""))
    if secrets.compare_digest(pw, _DASH_PW):
        _LOGIN_FAILS.pop(ip, None)
        tok = secrets.token_urlsafe(32)
        db.session_add(tok, now + _SESSION_TTL)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("cr_session", tok, max_age=_SESSION_TTL,
                        httponly=True, samesite="lax", secure=True, path="/")
        return resp
    fails.append(now)
    _LOGIN_FAILS[ip] = fails
    left = _MAX_FAILS - len(fails)
    return JSONResponse({"ok": False, "remaining": max(left, 0),
                         "error": f"비밀번호가 틀렸습니다. (남은 시도 {max(left, 0)}회)"},
                        status_code=401)


@app.post("/api/logout")
def api_logout(request: Request):
    db.session_del(request.cookies.get("cr_session", ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cr_session", path="/")
    return resp


def _strip_personal(r: dict):
    """게스트 응답에서 가구의 개인 소득/자격 판정을 제거한다.
    - AI 자격조언(rank_advice)·Q&A: 프로필 소득을 참조 → 제거
    공고 자체 공개정보(필요현금·분양가·납부일정·공고 소득기준 income_limit·지역순위)는 유지."""
    def clean(res):
        if isinstance(res, dict):
            res.pop("rank_advice", None)
    clean(r.get("llm"))
    for h in (r.get("llm_history") or []):
        clean(h.get("r"))
    r["qa"] = []
    # 가구 상황이 박힌 태그 제거: 소득 판정(소득/맞벌이) + 혼인상태(혼인/예비신혼/기혼)
    # + 생활권·관심(🚇 목적지 통근 = 출퇴근지, '서울 당해'·'서울 거주자' = 거주지, ⭐ 관심단지).
    _hide = ("소득", "맞벌이", "혼인", "예비신혼", "기혼",
             "목적지", "🚇", "통근", "당해", "거주자", "관심단지")
    r["tags"] = [t for t in (r.get("tags") or []) if not any(w in t for w in _hide)]
    # 메모(우리 판단 메모)·통근 분·LLM 메모(note)·알림끔/접기 표시도 개인 정보라 비운다.
    r["memo"] = ""
    r["commute_min"] = None
    r["muted"] = False
    r["collapsed"] = False
    for res in [r.get("llm")] + [h.get("r") for h in (r.get("llm_history") or [])]:
        if isinstance(res, dict):
            res.pop("note", None)


@app.get("/api/notices")
def api_notices(request: Request, all: bool = False):
    rows = db.list_notices(matched_only=not all)
    if not _authed(request):          # 게스트: 개인 소득 노출 방지
        for r in rows:
            _strip_personal(r)
    return rows


@app.get("/api/status")
def api_status(request: Request):
    cfg = rules.load_config()
    return {
        "last_sync": db.get_meta("last_sync"),
        "last_sync_counts": db.get_meta("last_sync_counts"),
        "mode": cfg.get("mode") if _authed(request) else None,  # 혼인상태는 게스트에 숨김
        "poll_interval_minutes": cfg.get("poll_interval_minutes"),
        "mock": os.environ.get("MOCK") == "1",
        "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "applyhome_key": bool(os.environ.get("APPLYHOME_API_KEY")),
        "lh_key": bool(os.environ.get("LH_API_KEY")),
        "today": datetime.now(KST).date().isoformat(),
    }


@app.post("/api/refresh")
def api_refresh(request: Request):
    if not _authed(request):
        return _forbid()
    return run_collection()


@app.post("/api/test-telegram")
def api_test_telegram(request: Request):
    if not _authed(request):
        return _forbid()
    ok = notify.send_telegram("✅ 청약 레이더 테스트 메시지입니다. 알림 연결 정상!")
    return JSONResponse({"sent": ok})


@app.get("/api/settings")
def api_get_settings(request: Request):
    """LLM 설정 조회. 비번/키는 노출하지 않고 설정 여부만. (전체 권한 전용)"""
    if not _authed(request):
        return _forbid()
    s = cost.llm_settings()
    return {
        "provider": s["provider"], "base_url": s["base_url"], "model": s["model"],
        "user": s["user"], "pass_set": bool(s["pass"]), "api_key_set": bool(s["api_key"]),
        "temperature": s["temperature"],
    }


@app.post("/api/settings")
def api_save_settings(request: Request, payload: dict = Body(...)):
    """대시보드에서 LLM 설정 저장(DB 영속). 비번/키는 빈값이면 기존 유지."""
    if not _authed(request):
        return _forbid()
    cost.save_llm_settings(payload or {})
    return {"saved": True}


@app.post("/api/mute")
def api_mute(request: Request, payload: dict = Body(...)):
    """알림 끄기/켜기(관심 없음). 끄면 알림 발송 제외 + 대시보드 접힘."""
    if not _authed(request):
        return _forbid()
    nid = (payload or {}).get("id")
    if not nid:
        return JSONResponse({"error": "no id"}, status_code=400)
    muted = bool((payload or {}).get("muted", True))
    db.set_muted(nid, muted)
    return {"id": nid, "muted": muted}


@app.post("/api/memo")
def api_memo(request: Request, payload: dict = Body(...)):
    """공고 메모 저장(빈 문자열이면 삭제)."""
    if not _authed(request):
        return _forbid()
    nid = (payload or {}).get("id")
    if not nid:
        return JSONResponse({"error": "no id"}, status_code=400)
    db.set_memo(nid, (payload or {}).get("text") or "")
    return {"id": nid, "memo": (payload or {}).get("text") or ""}


@app.post("/api/collapse")
def api_collapse(request: Request, payload: dict = Body(...)):
    """접기/펼치기. 알림은 계속 오고 대시보드만 1줄로 표시."""
    if not _authed(request):
        return _forbid()
    nid = (payload or {}).get("id")
    if not nid:
        return JSONResponse({"error": "no id"}, status_code=400)
    collapsed = bool((payload or {}).get("collapsed", True))
    db.set_collapsed(nid, collapsed)
    return {"id": nid, "collapsed": collapsed}


@app.post("/api/ask")
def api_ask(request: Request, payload: dict = Body(...)):
    """분석된 공고에 추가질문. stream=true면 SSE로 사고/답변을 실시간 중계."""
    if not _authed(request):
        return _forbid()
    nid = (payload or {}).get("id")
    question = ((payload or {}).get("question") or "").strip()
    n = db.get_notice(nid) if nid else None
    if not n:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not question:
        return JSONResponse({"error": "no question"}, status_code=400)
    if (payload or {}).get("stream"):
        return _ask_sse(n, question)
    try:
        answer = cost.ask(n, question, rules.load_config())
    except cost.LLMDownError as e:
        return {"answer": None, "error": "llm_down", "detail": str(e)[:200]}
    return {"answer": answer, "qa": db.list_qa(nid)}


def _ask_sse(notice, question):
    """추가질문을 스레드로 돌리고 진행 이벤트를 SSE로 흘려병낸다."""
    q = queue.Queue()

    def work():
        try:
            answer = cost.ask(notice, question, rules.load_config(),
                              on_event=lambda ev: q.put(ev))
            q.put({"done": bool(answer), "answer": answer, "qa": db.list_qa(notice["id"])})
        except cost.LLMDownError as e:
            q.put({"error": "llm_down", "detail": str(e)[:200]})
        except Exception as e:  # noqa: BLE001
            q.put({"error": str(e)[:200]})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── 서버 백그라운드 분석 잡 ──────────────────────────────────────
# 버튼을 누르면 데몬 스레드로 분석을 시작하고 즉시 반환한다. 브라우저를 닫거나
# 이동해도 서버가 끝까지 돌려 결과를 캐시하고, 대시보드는 /api/enrich-status를
# 폴링해 완료 시 카드를 자동 갱신한다. 같은 공고 중복 실행은 막는다.
_ENRICH_JOBS: dict = {}          # nid -> {"phase","done","error","started"}
_ENRICH_LOCK = threading.Lock()


def _do_enrich(notice, force=False) -> bool:
    """분석 1건 실행(잡 등록 + cost.enrich + 상태 갱신). 이미 진행 중이면 스킵.
    _start_enrich_job(버튼)과 자동 큐 워커가 공유한다."""
    nid = notice["id"]
    with _ENRICH_LOCK:
        job = _ENRICH_JOBS.get(nid)
        if job and not job["done"]:
            return False  # 이미 진행 중 → 중복 실행 방지
        for k in [k for k, j in _ENRICH_JOBS.items() if j["done"]]:  # 완료 잡 정리(메모리)
            _ENRICH_JOBS.pop(k, None)
        _ENRICH_JOBS[nid] = {"phase": "분석 대기 중…", "done": False, "error": None,
                             "started": db.now_kst()}
    try:
        def on_ev(ev):
            if isinstance(ev, dict) and ev.get("phase"):
                _ENRICH_JOBS[nid]["phase"] = ev["phase"]
        result = cost.enrich(notice, rules.load_config(), force, on_event=on_ev)
        if isinstance(result, dict) and result.get("error"):
            _ENRICH_JOBS[nid]["error"] = result["error"]  # LLM 서버 죽음 등(캐시 안 됨 → 재시도됨)
    except Exception as e:  # noqa: BLE001
        log.warning("%s 분석 실패: %s", notice.get("name"), e)
        _ENRICH_JOBS[nid]["error"] = str(e)[:200]
    finally:
        _ENRICH_JOBS[nid]["done"] = True
    return True


def _start_enrich_job(notice, force) -> bool:
    """버튼용: 데몬 스레드로 즉시 실행(응답성)."""
    threading.Thread(target=_do_enrich, args=(notice, force), daemon=True).start()
    return True


# ── 자동 분석: 새 공고/미분석 공고를 서버에서 순차 분석(LLM 실행 안 된 게 없도록) ──
# 한 번에 다 돌리면 LLM 서버(Kimi/gemma4)에 부담 → 워커 1개로 직렬 처리.
# 실패(LLM 다운 등)는 캐시되지 않으므로 다음 수집 때 다시 큐에 올라가 재시도된다.
_AUTO_Q: "queue.Queue" = queue.Queue()
_AUTO_QUEUED: set = set()


def _auto_worker():
    while True:
        nid = _AUTO_Q.get()
        _AUTO_QUEUED.discard(nid)
        try:
            if cost._cache_get(nid) != "miss":   # 이미 분석됨(성공/정보없음) → 스킵
                continue
            n = db.get_notice(nid)
            if n:
                _do_enrich(n, force=False)
        except Exception as e:  # noqa: BLE001
            log.warning("자동 분석 실패(%s): %s", nid, e)


def _enqueue_backfill():
    """분석 이력이 없는 matched 공고(신규 포함)를 자동 분석 큐에 적재. LLM 미설정이면 건너뜀."""
    try:
        if not cost.llm_settings().get("provider"):
            return
        n_added = 0
        for r in db.list_notices(matched_only=True):
            nid = r["id"]
            if r.get("enriched") or nid in _AUTO_QUEUED:   # enriched = 분석 캐시 있음
                continue
            _AUTO_QUEUED.add(nid)
            _AUTO_Q.put(nid)
            n_added += 1
        if n_added:
            log.info("자동 분석 큐 적재: %d건", n_added)
    except Exception as e:  # noqa: BLE001
        log.warning("자동 분석 큐 적재 실패: %s", e)


@app.post("/api/enrich")
def api_enrich(request: Request, payload: dict = Body(...)):
    """공고 1건 LLM 분석. 기본은 서버 백그라운드 잡으로 시작하고 즉시 반환한다
    (브라우저 닫아도 서버가 끝까지 실행·캐시 → 대시보드가 폴링해 자동 반영).
    stream=true면 기존 SSE 실시간 중계도 지원한다."""
    if not _authed(request):  # 게스트는 분석(DB 캐시 기록) 불가
        return _forbid()
    nid = (payload or {}).get("id")
    n = db.get_notice(nid) if nid else None
    if not n:
        return JSONResponse({"error": "not found"}, status_code=404)
    if (payload or {}).get("stream"):
        return _enrich_sse(n, bool((payload or {}).get("force")))
    _start_enrich_job(n, bool((payload or {}).get("force")))
    return {"started": True, "running": True}


@app.get("/api/enrich-status")
def api_enrich_status(request: Request, id: str):
    """백그라운드 분석 진행/완료 상태. 완료면 결과(llm)+히스토리를 함께 반환.
    결과에 개인 소득이 담긴 자격조언이 있어 전체 권한 전용(게스트는 분석 자체를 못 함)."""
    if not _authed(request):
        return _forbid()
    job = _ENRICH_JOBS.get(id)
    if job and not job["done"]:
        return {"running": True, "done": False, "phase": job.get("phase")}
    if job and job["done"] and job.get("error"):
        return {"running": False, "done": True, "error": job["error"]}
    cached = cost._cache_get(id)   # dict / None(실패) / "miss"
    if job and job["done"]:
        return {"running": False, "done": True,
                "llm": None if cached == "miss" else cached,
                "llm_history": cost.cache_history(id)}
    if cached != "miss":           # 이전에 이미 분석됨
        return {"running": False, "done": True, "llm": cached,
                "llm_history": cost.cache_history(id)}
    return {"running": False, "done": False}


@app.get("/api/enrich-jobs")
def api_enrich_jobs():
    """서버에서 진행 중인 분석 공고 id 목록(대시보드 재접속 시 진행표시 복원용)."""
    return {"running": [nid for nid, j in _ENRICH_JOBS.items() if not j["done"]]}


def _enrich_sse(notice, force):
    """분석을 스레드로 돌리고 진행 이벤트를 SSE로 흘려보낸다.
    델타가 계속 흐르뮼로 프록시 60s 아이들 타임아웃도 우회된다."""
    q = queue.Queue()

    def work():
        try:
            result = cost.enrich(notice, rules.load_config(), force,
                                 on_event=lambda ev: q.put(ev))
            if isinstance(result, dict) and result.get("error"):
                q.put({"error": result["error"]})
            else:
                q.put({"done": bool(result), "llm": result,
                       "llm_history": cost.cache_history(notice["id"])})
        except Exception as e:  # noqa: BLE001
            q.put({"error": str(e)[:200]})
        finally:
            q.put(None)  # 스트림 종료 신호

    threading.Thread(target=work, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
