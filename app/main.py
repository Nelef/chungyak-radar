"""청약 레이더 — 수집 스케줄러 + 대시보드 서버.

실행: uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

# 이미지에 구운 /srv/.env(키·설정)를 로드해 os.environ에 채운다.
# 키를 이미지 ENV로 노출하지 않아 Container Manager가 환경변수를 물어보지 않는다.
# 파일이 없으면(compose에서 env_file로 주입하는 등) 조용히 무시된다. 셸/도커가
# 이미 설정한 값은 덮어쓰지 않는다(override=False 기본).
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse

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
def api_refresh():
    return run_collection()


@app.post("/api/test-telegram")
def api_test_telegram():
    ok = notify.send_telegram("✅ 청약 레이더 테스트 메시지입니다. 알림 연결 정상!")
    return JSONResponse({"sent": ok})


@app.get("/api/settings")
def api_get_settings():
    """LLM 설정 조회. 비번/키는 노출하지 않고 설정 여부만."""
    s = cost.llm_settings()
    return {
        "provider": s["provider"], "base_url": s["base_url"], "model": s["model"],
        "user": s["user"], "pass_set": bool(s["pass"]), "api_key_set": bool(s["api_key"]),
        "temperature": s["temperature"],
    }


@app.post("/api/settings")
def api_save_settings(payload: dict = Body(...)):
    """대시보드에서 LLM 설정 저장(DB 영속). 비번/키는 빈값이면 기존 유지."""
    cost.save_llm_settings(payload or {})
    return {"saved": True}


@app.post("/api/mute")
def api_mute(payload: dict = Body(...)):
    """알림 끄기/켜기(관심 없음). 끄면 알림 발송 제외 + 대시보드 접힘."""
    nid = (payload or {}).get("id")
    if not nid:
        return JSONResponse({"error": "no id"}, status_code=400)
    muted = bool((payload or {}).get("muted", True))
    db.set_muted(nid, muted)
    return {"id": nid, "muted": muted}


@app.post("/api/enrich")
def api_enrich(payload: dict = Body(...)):
    """공고 1건 온디맨드 LLM 분석 (분양=필요현금+순위조언 / 임대=신청기간+임대료)."""
    nid = (payload or {}).get("id")
    n = db.get_notice(nid) if nid else None
    if not n:
        return JSONResponse({"error": "not found"}, status_code=404)
    result = cost.enrich(n, rules.load_config(), bool((payload or {}).get("force")))
    if isinstance(result, dict) and result.get("error"):
        # LLM 서버 죽음 등: 분석 완료로 표시하지 않음(공고 유지 + 재시도 가능)
        return {"enriched": False, "llm": None, "error": result["error"]}
    return {"enriched": True, "llm": result, "llm_history": cost.cache_history(nid)}
