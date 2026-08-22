"""IndexNow: сообщаем поисковику о новой странице сразу, не ждём планового обхода.

Ключ фиксированный, а не генерируется на старте: IndexNow проверяет его,
скачивая файл <key>.txt с домена при каждой отправке, и должен быть стабилен
между деплоями. Смена ключа не ломает сайт, просто обнуляет историю проверки
на стороне Яндекса/Bing — поэтому меняем только осознанно, через переменную
окружения, а не как побочный эффект рестарта.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

KEY = os.getenv("INDEXNOW_KEY", "c8c4a488d19c58b4c6363b4a9b109594")
ENDPOINT = "https://api.indexnow.org/indexnow"


def submit(domain: str, paths: list[str]) -> bool:
    """Отправить список путей. True при успехе. Никогда не бросает — вызывается
    из фоновых задач и скриптов, где сорванная отправка не должна ронять
    остальной процесс."""
    try:
        r = httpx.post(ENDPOINT, json={
            "host": domain,
            "key": KEY,
            "keyLocation": f"https://{domain}/{KEY}.txt",
            "urlList": [f"https://{domain}{p}" for p in paths],
        }, timeout=15)
        ok = r.status_code in (200, 202)
        (log.info if ok else log.warning)("IndexNow %s: %s путей, код %s",
                                          domain, len(paths), r.status_code)
        return ok
    except httpx.HTTPError as e:
        log.warning("IndexNow недоступен: %s", e)
        return False
