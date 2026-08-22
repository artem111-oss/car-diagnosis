"""Свободный текст владельца -> канонический класс cause.

Владелец пишет в /api/feedback своими словами: «оказались направляющие
клапанов», «это был подшипник ступицы». Модель обучена на 21 классе из
labels_ru.PARTS (вокабуляр снят прямо с models/best_model_clap.joblib, не из
документации), и `cardiag ingest --cause <класс>` ждёт ровно один из этих
ключей, а не произвольный текст.

Здесь этот текст размечается дешёвой LLM-классификацией — без неё каждое
подтверждение так и осталось бы текстом, непригодным для дообучения.
Направляющие клапанов из этого файла в общей таксономии отдельного класса не
имеют — ближайший по смыслу это valvetrain (там же гидрокомпенсаторы и
зазоры), и классификатору явно разрешено округлять до ближайшего класса, а не
требовать точного совпадения.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

from service.labels_ru import PARTS

log = logging.getLogger(__name__)

API_URL = os.getenv("AIMLAPI_URL", "https://api.aimlapi.com/v1/chat/completions")
MODEL = os.getenv("LABEL_MAPPER_MODEL", "openai/gpt-4o-mini")
TIMEOUT = float(os.getenv("LABEL_MAPPER_TIMEOUT", "20"))

NORMAL = "normal"  # сентинел: владелец подтвердил, что неисправности не было

# Один класс на узел (bad_wheal_bearing — опечатка в обучающем корпусе
# апстрима, дублирует wheel_bearing); классификатору нет смысла путать эти два.
_CANONICAL = sorted({k for k in PARTS if k not in ("bad_wheal_bearing", "other")})


def api_key() -> str:
    return os.getenv("AIMLAPI_KEY", "").strip()


def _catalogue() -> str:
    return "\n".join(f"- {key}: {PARTS[key].name} — {PARTS[key].hint}"
                     for key in _CANONICAL)


SYSTEM = f"""Ты сопоставляешь свободный текст владельца автомобиля с одним
каноническим классом неисправности из фиксированного списка. Список закрыт —
на выходе годится только один из этих ключей, "{NORMAL}" или null.

Классы:
{_catalogue()}

"{NORMAL}" — если владелец подтвердил, что неисправности не было (звук
оказался посторонним, ложное срабатывание).

null — если текст не описывает ни один из классов достаточно уверенно, или
слишком неконкретен ("мастер посмотрел", "починили").

Правила:
1. Округляй до ближайшего класса по смыслу, а не требуй точного совпадения
   термина. «Направляющие клапанов» и «маслосъёмные колпачки» — это valvetrain
   (там же гидрокомпенсаторы и зазоры, весь газораспределительный механизм).
   «Хрустит при повороте» без другого контекста — cv_axle.
2. Не выдумывай уверенность: если сомневаешься между двумя классами или текст
   расплывчатый, возвращай null, а не гадай.
3. Отвечай только JSON, без markdown и пояснений вокруг:
   {{"label": "ключ_класса" | "{NORMAL}" | null, "confidence": "high" | "medium" | "low"}}"""


def _parse(txt: str) -> dict:
    s = txt.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?", "", s).removesuffix("```").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(s[start:end + 1])


def classify(owner_text: str, *, symptom: str = "", ai_status: str = "",
            ai_top_part: str = "") -> tuple[str | None, str]:
    """Вернуть (канонический_класс_или_None, уверенность).

    Никогда не бросает: неразмеченная запись просто не попадёт в экспорт для
    обучения, а не сломает сохранение фидбека.
    """
    owner_text = owner_text.strip()
    if not owner_text or not api_key():
        return None, "low"

    user = (
        f"Симптом со слов владельца при записи: {symptom or 'не указан'}\n"
        f"Вердикт акустической модели: {ai_status or 'неизвестен'}\n"
        f"Версия ИИ-механика (может быть неверной): {ai_top_part or 'нет'}\n\n"
        f"Что владелец сообщил после посещения сервиса: {owner_text}"
    )

    try:
        r = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": user}],
                "temperature": 0.0,
                "max_tokens": 150,
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        log.warning("классификатор меток недоступен: %s", e)
        return None, "low"

    if r.status_code != 200:
        log.warning("классификатор меток вернул %s: %s", r.status_code, r.text[:200])
        return None, "low"

    try:
        data = _parse(r.json()["choices"][0]["message"]["content"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("не разобрали ответ классификатора: %s", e)
        return None, "low"

    label = data.get("label")
    if label not in _CANONICAL and label != NORMAL:
        if label is not None:
            log.warning("классификатор вернул класс не из списка: %r", label)
        label = None
    return label, str(data.get("confidence", "low"))
