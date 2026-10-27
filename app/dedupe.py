"""같은 모집을 가리키는 공고 묶기 — NAS의 Claude(개인 구독)가 판단하고, 결과는 DB(meta)에 캐시.

정정공고 · 재공고 · 회차 · 블록 분리 · 청약홈/LH 중복처럼 이름 규칙으로는 다 못 잡는 경우가 많아서
(사용자 요청 2026-10-27) 공고 목록이 바뀔 때마다 Claude에게 묶음과 짧은 제목을 받아 둔다.
캘린더 피드(ical.py)와 아침 리마인더(main.run_reminders)가 이 묶음을 쓴다.
Claude가 없거나 실패하면 묶음 없이(각 공고 그대로) 동작하고, ical.py는 이름 규칙으로 정리한다.
"""
import hashlib
import json
import logging
import re
from datetime import date, timedelta

from app import cost, db

log = logging.getLogger("dedupe")

_META = "dedupe_groups"
_FIELDS = ("name", "house_kind", "region", "notice_date", "sp_start", "sp_end", "r1_start", "r1_end", "winner_date")

_PROMPT = """아래는 청약 공고 목록(JSON 배열)이다. 같은 모집(같은 단지의 같은 접수)을 가리키는 공고끼리 묶어라.
- 묶는 경우: 정정공고 · 재공고 · 추가 안내처럼 같은 모집을 다시 올린 공고, 같은 단지 같은 일정의 회차/블록,
  같은 단지가 청약홈과 LH 등 다른 출처로 중복 수집된 경우
- 따로 두는 경우: 단지가 다르거나(예: 3단지와 4단지) 접수 일정이 다른 별개 모집(1차 · 2차 일정이 다르면 따로)
- keep: 묶음의 대표 id. 정정공고가 있으면 공고일(notice_date)이 가장 늦은 것, 아니면 정보가 가장 많은 것
- title: 달력 · 알림에 쓸 짧은 이름(단지명 중심, 15~25자). '[정정공고]' · '입주자모집공고' · 공고일 같은 군더더기는 빼고,
  무순위 · 신혼희망타운 · 행복주택처럼 유형을 구분하는 말은 앞에 짧게 남긴다
- 모든 id가 정확히 한 묶음에 들어가야 한다(혼자인 공고도 묶음 하나)
JSON만 출력: {"groups":[{"ids":["..."],"keep":"...","title":"..."}]}

공고 목록:
"""


def _window(n: dict, today: date) -> bool:
    ds = [n.get(k) for k in ("sp_start", "sp_end", "r1_start", "r1_end", "winner_date", "notice_date")]
    ds = [d for d in ds if d]
    return bool(ds) and max(ds)[:10] >= (today - timedelta(days=14)).isoformat()


def _items(notices: list[dict], today: date) -> list[dict]:
    return [{"id": n["id"], **{k: n.get(k) for k in _FIELDS if n.get(k)}}
            for n in notices if not n.get("muted") and _window(n, today)]


def _sig(items: list[dict]) -> str:
    return hashlib.sha1(json.dumps(items, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _parse(text: str, ids: set) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    groups = json.loads(m.group(0))["groups"] if m else []
    out, seen = [], set()
    for g in groups:
        gi = [i for i in g.get("ids", []) if i in ids and i not in seen]
        if not gi:
            continue
        seen.update(gi)
        keep = g.get("keep") if g.get("keep") in gi else gi[0]
        out.append({"ids": gi, "keep": keep, "title": (g.get("title") or "").strip()[:40]})
    out += [{"ids": [i], "keep": i, "title": ""} for i in sorted(ids - seen)]
    return out


def refresh(notices: list[dict], today: date) -> bool:
    """공고 목록이 바뀌었으면 Claude로 다시 묶는다. 바뀐 게 없거나 Claude가 없으면 그대로 둔다."""
    items = _items(notices, today)
    if not items:
        return False
    sig = _sig(items)
    cur = json.loads(db.get_meta(_META) or "{}")
    if cur.get("sig") == sig:
        return False
    if not cost.claude_available():
        return False
    try:
        text = cost._claude_generate(_PROMPT + json.dumps(items, ensure_ascii=False), timeout=240)
        groups = _parse(text, {i["id"] for i in items})
    except Exception as e:  # noqa: BLE001 — 실패하면 이전 묶음/이름 규칙으로 계속 동작
        log.warning("공고 묶기 실패: %s", e)
        return False
    db.set_meta(_META, json.dumps({"sig": sig, "at": db.now_kst(), "groups": groups}, ensure_ascii=False))
    merged = sum(len(g["ids"]) - 1 for g in groups)
    log.info("공고 묶기: %d건 → %d묶음 (중복 %d건 정리)", len(items), len(groups), merged)
    return True


def groups_by_id() -> dict:
    """id → {"keep", "title", "ids"} (캐시된 묶음; 없으면 빈 dict)."""
    cur = json.loads(db.get_meta(_META) or "{}")
    return {i: g for g in cur.get("groups", []) for i in g["ids"]}
