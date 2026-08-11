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
    sched.add_job(run_collection, "interval", minutes=cfg["poll_interval_minutes"],
                  next_run_time=datetime.now(KST) + timedelta(seconds=5))
    sched.add_job(run_reminders, "cron", hour=8, minute=30)  # 매일 아침 8:30 리마인더
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
_SESSIONS: dict = {}          # token -> expiry(epoch)
_LOGIN_FAILS: dict = {}       # ip -> [fail timestamps]
_SESSION_TTL = 30 * 86400
_MAX_FAILS, _FAIL_WINDOW = 5, 600   # 무차별 대입 방지: 10분에 5회 실패 시 잠금


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _authed(request: Request) -> bool:
    if not _DASH_PW:            # 비번 미설정 = 게이트 없음(전원 허용)
        return True
    tok = request.cookies.get("cr_session", "")
    exp = _SESSIONS.get(tok)
    if not exp:
        return False
    if exp < time.time():
        _SESSIONS.pop(tok, None)
        return False
    return True


def _forbid():
    return JSONResponse({"error": "forbidden",
                         "detail": "로그인이 필요합니다 (게스트는 읽기 전용)."}, status_code=403)


@app.get("/api/whoami")
def api_whoami(request: Request):
    """현재 세션 권한. 대시보드가 게이트를 띄울지 판단."""
    return {"authed": _authed(request), "gated": bool(_DASH_PW)}


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
        _SESSIONS[tok] = now + _SESSION_TTL
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
    _SESSIONS.pop(request.cookies.get("cr_session", ""), None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cr_session", path="/")
    return resp


@app.get("/api/notices")
def api_notices(all: bool = False):
    return db.list_notices(matched_only=not all)


@app.get("/api/status")
def api_status():
    cfg = rules.load_config()
    return {
        "last_sync": db.get_meta("last_sync"),
        "last_sync_counts": db.get_meta("last_sync_counts"),
        "mode": cfg.get("mode"),
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


def _start_enrich_job(notice, force) -> bool:
    nid = notice["id"]
    with _ENRICH_LOCK:
        job = _ENRICH_JOBS.get(nid)
        if job and not job["done"]:
            return True  # 이미 서버에서 진행 중 → 중복 실행 방지
        # 오래된 완료 잡 정리(메모리)
        for k in [k for k, j in _ENRICH_JOBS.items() if j["done"]]:
            _ENRICH_JOBS.pop(k, None)
        _ENRICH_JOBS[nid] = {"phase": "분석 대기 중…", "done": False, "error": None,
                             "started": db.now_kst()}

    def work():
        try:
            def on_ev(ev):
                if isinstance(ev, dict) and ev.get("phase"):
                    _ENRICH_JOBS[nid]["phase"] = ev["phase"]
            result = cost.enrich(notice, rules.load_config(), force, on_event=on_ev)
            if isinstance(result, dict) and result.get("error"):
                _ENRICH_JOBS[nid]["error"] = result["error"]  # LLM 서버 죽음 등(캐시 안 됨)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 백그라운드 분석 실패: %s", notice.get("name"), e)
            _ENRICH_JOBS[nid]["error"] = str(e)[:200]
        finally:
            _ENRICH_JOBS[nid]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return True


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
def api_enrich_status(id: str):
    """백그라운드 분석 진행/완료 상태. 완료면 결과(llm)+히스토리를 함께 반환."""
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
