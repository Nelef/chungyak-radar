"""텔레그램 알림. 토큰 미설정 시 로그로만 출력(드라이런)."""
import logging
import os

import requests

log = logging.getLogger("notify")


def send_telegram(text: str) -> bool:
    if os.environ.get("DEMO_MODE", "") == "1":   # 공개 데모는 이미지에 토큰이 있어도 절대 발송 안 함
        return False
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.info("[드라이런] 텔레그램 미설정, 발송 생략:\n%s", text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("텔레그램 발송 실패: %s", e)
        return False
