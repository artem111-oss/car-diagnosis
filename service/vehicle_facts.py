"""Проверенные факты об автомобиле — опора для механика.

Зачем это нужно, видно на живой ошибке. На Lada Granta с мотором 11183 стоит
ремень ГРМ, а модель предположила растянутую цепь и построила на ней цепочку
рассуждений про датчик положения распредвала. Детали, которой в машине нет,
достаточно, чтобы весь разбор оказался мусором.

Ловить такие выдумки постфактум поздно: пользователь уже прочитал. Поэтому
факты подтягиваются заранее и уходят в промпт как жёсткая опора.

Источник — Perplexity Sonar Pro: он ищет в вебе и отдаёт ссылки, то есть
отвечает на вопрос «ремень или цепь» не по памяти, а по документации.

Кэш по (марка, модель, год) обязателен: массовых машин немного, и один и тот
же запрос иначе оплачивался бы на каждом анализе.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading

import httpx

log = logging.getLogger(__name__)

API_URL = os.getenv("AIMLAPI_URL", "https://api.aimlapi.com/v1/chat/completions")
FACTS_MODEL = os.getenv("FACTS_MODEL", "perplexity/sonar-pro")
FACTS_TIMEOUT = float(os.getenv("FACTS_TIMEOUT", "45"))
FACTS_MAX_TOKENS = int(os.getenv("FACTS_MAX_TOKENS", "1200"))
CACHE_MAX = int(os.getenv("FACTS_CACHE_MAX", "400"))

_cache: dict[str, dict] = {}
_lock = threading.Lock()


def api_key() -> str:
    return os.getenv("AIMLAPI_KEY", "").strip()


PROMPT = """Ты — справочник по устройству автомобилей. Отвечай только тем, что
можешь подтвердить источниками, и честно пиши "неизвестно", если данных нет.
Не строй догадок: этот ответ пойдёт в основу диагноза, и выдуманная деталь
испортит его целиком.

Автомобиль: {vehicle}

Ответь строго JSON:
{{
 "engines": "какие двигатели ставились на эту модель в этом поколении, кратко",
 "timing_drive": "ремень | цепь | зависит от двигателя — и обязательно уточни, от какого",
 "timing_note": "одна фраза: интервал замены, чем грозит обрыв",
 "hydraulic_lifters": "есть гидрокомпенсаторы или зазоры регулируются вручную",
 "known_issues": ["типовая болячка на пробеге 100-200 тыс. км", "ещё одна"],
 "not_applicable": ["узлы, которых на этой машине НЕТ и которые нельзя называть в диагнозе"],
 "confidence": "high | medium | low"
}}"""


def _key(vehicle: dict) -> str:
    return " ".join(str(vehicle.get(k, "")).strip().lower()
                    for k in ("brand", "model", "year")).strip()


def _vehicle_line(vehicle: dict) -> str:
    bits = [vehicle.get("brand", ""), vehicle.get("model", ""), vehicle.get("year", "")]
    return " ".join(b for b in bits if b).strip()


def _parse(txt: str) -> dict:
    s = txt.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?", "", s).removesuffix("```").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(s[start:end + 1])


def _fetch(vehicle: dict) -> dict:
    """Один заход к справочнику. Пустой словарь на любой неудаче."""
    try:
        r = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json={
                "model": FACTS_MODEL,
                "messages": [{"role": "user",
                              "content": PROMPT.format(vehicle=_vehicle_line(vehicle))}],
                "temperature": 0.1,
                "max_tokens": FACTS_MAX_TOKENS,
                # response_format здесь не задаём: json_object Perplexity
                # отклоняет, а json_schema в этом шлюзе тоже не проходит
                # валидацию. Формат держится инструкцией в промпте, надёжность
                # добирает повтор в lookup() — поисковая модель изредка
                # отвечает прозой со ссылками вместо JSON.
            },
            timeout=FACTS_TIMEOUT,
        )
    except httpx.HTTPError as e:
        log.warning("справочник недоступен: %s", e)
        return {}

    if r.status_code != 200:
        log.warning("справочник вернул %s: %s", r.status_code, r.text[:200])
        return {}

    try:
        return _parse(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("не разобрали ответ справочника: %s", e)
        return {}


def lookup(vehicle: dict) -> dict:
    """Факты об автомобиле. Пустой словарь, если марка не указана или поиск не удался.

    Никогда не бросает: разбор должен состояться и без справки, просто с
    оговоркой для механика, что проверенных данных нет.
    """
    key = _key(vehicle)
    if not key or not vehicle.get("brand"):
        return {}

    with _lock:
        if key in _cache:
            return _cache[key]

    if not api_key():
        return {}

    # Sonar — поисковая модель и охотно отвечает прозой со ссылками вместо
    # JSON. response_format прижимает её к формату, повтор добирает остаток:
    # без него разбор тянул справку заново и терял на этом секунды.
    facts = _fetch(vehicle) or _fetch(vehicle)
    if not facts:
        return {}

    with _lock:
        _cache[key] = facts
        while len(_cache) > CACHE_MAX:
            _cache.pop(next(iter(_cache)))

    log.info("справочник: %s -> привод ГРМ %s, уверенность %s",
             key, facts.get("timing_drive", "?"), facts.get("confidence", "?"))
    return facts


def as_prompt_block(facts: dict) -> str:
    """Факты в виде куска промпта. Пустая строка, если справки нет."""
    if not facts:
        return ("Проверенных данных по этой машине нет. Не утверждай ничего про "
                "её устройство — про привод ГРМ, тип клапанного механизма и "
                "прочее — если не уверен. Лучше спроси у владельца.")

    lines = ["ПРОВЕРЕННЫЕ ДАННЫЕ ОБ ЭТОЙ МАШИНЕ (опирайся на них, не противоречь):"]
    if facts.get("engines"):
        lines.append(f"- двигатели: {facts['engines']}")
    if facts.get("timing_drive"):
        lines.append(f"- привод ГРМ: {facts['timing_drive']}")
    if facts.get("timing_note"):
        lines.append(f"  ({facts['timing_note']})")
    if facts.get("hydraulic_lifters"):
        lines.append(f"- клапанной механизм: {facts['hydraulic_lifters']}")
    if facts.get("known_issues"):
        lines.append("- типовые болячки: " + "; ".join(str(x) for x in facts["known_issues"][:5]))
    if facts.get("not_applicable"):
        lines.append("- ЭТОГО НА МАШИНЕ НЕТ, называть нельзя: "
                     + "; ".join(str(x) for x in facts["not_applicable"][:5]))
    if facts.get("confidence") in ("low", "medium"):
        lines.append(f"- достоверность справки: {facts['confidence']}, "
                     "спорные места уточняй у владельца")
    return "\n".join(lines)
