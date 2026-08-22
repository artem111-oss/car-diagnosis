"""Второе мнение от LLM поверх акустического анализа.

Акустическая модель надёжно определяет зону (правильная в топ-3 примерно в 75%
случаев) и плохо — конкретную деталь: голова cause 21-классовая и не
откалибрована. Но у неё нет доступа к тому, что знает механик: типовые болячки
конкретной модели на конкретном пробеге и характер симптома со слов владельца.
Этот разрыв и закрывает LLM.

Наружу уходят только числа акустической модели, марка, пробег и описание
симптома. Само аудио остаётся на нашем сервере: так записи пользователей не
покидают инфраструктуру, что важно и для доверия, и для 152-ФЗ.

Модель выбрана замером на подтверждённом случае (Lada Granta 150 тыс. км,
цоканье ГРМ): claude-haiku-latest дал валидный JSON, верно объяснил, почему
звук тише под нагрузкой, и честно обозначил пределы вывода. qwen3.7-plus
показал сопоставимое качество вчетверо дешевле и оставлен как альтернатива
через AIMLAPI_MODEL.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

import httpx

log = logging.getLogger(__name__)

API_URL = os.getenv("AIMLAPI_URL", "https://api.aimlapi.com/v1/chat/completions")
MODEL = os.getenv("AIMLAPI_MODEL", "anthropic/claude-haiku-latest")
TIMEOUT = float(os.getenv("AIMLAPI_TIMEOUT", "60"))
MAX_TOKENS = int(os.getenv("AIMLAPI_MAX_TOKENS", "1600"))


def api_key() -> str:
    """Читаем при каждом вызове: ключ можно подменить без перезапуска."""
    return os.getenv("AIMLAPI_KEY", "").strip()


SYSTEM = """Ты — автомеханик-диагност с 20-летним стажем, работающий в России.
Ты знаешь типовые болячки массовых на российском рынке машин: Lada, Kia,
Hyundai, Renault, Volkswagen, Toyota, а также китайских Haval, Chery, Geely.

Ты работаешь в паре с акустической моделью. Разделение труда такое:
- ЗОНЕ неисправности от неё доверяй: правильная зона попадает в топ-3 примерно
  в 75% случаев.
- Её версию по КОНКРЕТНОЙ детали считай только намёком: эта голова
  21-классовая, не откалибрована, и её проценты не являются вероятностями.
- Твоя ценность в том, чего модель не знает: типовые неисправности этой марки
  на этом пробеге и характер симптома со слов владельца.

Правила:
1. Не выдумывай уверенность. Если улик мало, скажи об этом прямо.
2. Опирайся на связь симптома с механикой. Объясняй, почему звук ведёт себя
   именно так (меняется с оборотами, с нагрузкой, с температурой).
3. Цены давай в рублях, реалистичные для России, с учётом работы.
4. Если подозреваешь узел безопасности (тормоза, рулевое, подвеска) — ставь
   высокую срочность.
5. Пиши для владельца машины, а не для механика. Термин используй, только если
   без него теряется смысл, и поясняй его.
6. НЕ приводи числовые вероятности в тексте для пользователя. Цифры вроде
   "0.924" тебе даны как рабочая информация, но они не откалиброваны, и мы
   намеренно не показываем их владельцу. Говори словами: "звук характерен для",
   "менее вероятно", "почти исключено".
7. Задай 1-3 уточняющих вопроса, ответы на которые сузили бы поиск.
8. Возвращай ТОЛЬКО JSON по схеме, без markdown и пояснений вокруг."""

SCHEMA_HINT = """{
 "diagnosis": "вывод одной фразой, понятной владельцу",
 "reasoning": "2-3 предложения: почему именно эти узлы, как симптом связан с механикой",
 "parts": [
   {"name": "узел", "why": "почему подходит под симптом и пробег",
    "likelihood": "высокая|средняя|низкая",
    "check": "как проверить, желательно самому",
    "cost_rub": "диапазон с работой, например 3000-8000"}
 ],
 "urgency": "critical|high|medium|low",
 "urgency_why": "чем рискует владелец, если тянуть",
 "diy_checks": ["что можно проверить самому за 5 минут"],
 "questions": ["уточняющий вопрос владельцу"],
 "confidence_note": "честно о пределах вывода"
}"""


@dataclass
class MechanicOpinion:
    ok: bool = False
    diagnosis: str = ""
    reasoning: str = ""
    parts: list[dict] = field(default_factory=list)
    urgency: str = ""
    urgency_why: str = ""
    diy_checks: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    confidence_note: str = ""
    model: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _vehicle_line(v: dict) -> str:
    bits = [v.get("brand", ""), v.get("model", ""), v.get("year", "")]
    name = " ".join(b for b in bits if b).strip() or "марка не указана"
    km = v.get("mileage", "")
    return f"{name}, пробег {km} км" if km else name


def _prompt(report: dict, vehicle: dict, symptom: str) -> str:
    dbg = report.get("debug", {})
    regions = ", ".join(
        f"{r['zone']} {r['p']}" for r in dbg.get("regions", [])[:3]) or "нет"
    causes = ", ".join(
        f"{c['part']} {c['p']}" for c in dbg.get("causes", [])[:3]) or "нет"

    lines = [
        "Результат акустического анализа записи:",
        f"- вердикт: {report.get('status')}",
        f"- зоны (надёжно): {regions}",
        f"- намёки по деталям (НЕ калибровано, только подсказка): {causes}",
        f"- вероятность детонационного стука: {dbg.get('knock_probability')}",
        f"- калиброванная уверенность: {report.get('band')} "
        f"({report.get('band_gloss', '')})",
        f"- чистого механического звука: {report.get('clean_seconds')} сек "
        f"из {report.get('total_seconds')}",
    ]
    if report.get("conflict"):
        lines.append("- ВНИМАНИЕ: зона и деталь от модели противоречат друг другу, "
                     "поэтому детали от неё доверять нельзя вовсе")

    lines += [
        "",
        f"Автомобиль: {_vehicle_line(vehicle)}",
        f"Симптом со слов владельца: {symptom or 'не описан'}",
        "",
        "Верни JSON строго по схеме:",
        SCHEMA_HINT,
    ]
    return "\n".join(lines)


def _parse(txt: str) -> dict:
    """Модели иногда оборачивают JSON в markdown, несмотря на инструкцию."""
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        s = s.removeprefix("json").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("в ответе нет JSON")
    return json.loads(s[start:end + 1])


def ask(report: dict, vehicle: dict, symptom: str) -> MechanicOpinion:
    """Спросить механика. Никогда не бросает: акустический результат уже есть,
    и падение LLM не должно ломать основной ответ пользователю."""
    key = api_key()
    if not key:
        return MechanicOpinion(error="AIMLAPI_KEY не задан")

    # На неуверенном вердикте рассуждать не о чем: модель начнёт придумывать,
    # а мы заплатим за токены. Экономит деньги и убирает источник выдумок.
    if report.get("status") == "uncertain":
        return MechanicOpinion(error="акустика не дала опоры для разбора")

    try:
        r = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _prompt(report, vehicle, symptom)},
                ],
                "temperature": 0.2,
                "max_tokens": MAX_TOKENS,
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        log.warning("AIMLAPI недоступен: %s", e)
        return MechanicOpinion(error="сервис разбора временно недоступен")

    if r.status_code != 200:
        log.warning("AIMLAPI %s: %s", r.status_code, r.text[:200])
        return MechanicOpinion(error=f"сервис разбора вернул {r.status_code}")

    try:
        body = r.json()
        data = _parse(body["choices"][0]["message"]["content"])
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        log.warning("не разобрали ответ LLM: %s", e)
        return MechanicOpinion(error="не удалось разобрать ответ")

    parts = [p for p in data.get("parts", []) if isinstance(p, dict)][:5]
    return MechanicOpinion(
        ok=True,
        diagnosis=str(data.get("diagnosis", "")),
        reasoning=str(data.get("reasoning", "")),
        parts=parts,
        urgency=str(data.get("urgency", "")),
        urgency_why=str(data.get("urgency_why", "")),
        diy_checks=[str(x) for x in data.get("diy_checks", [])][:5],
        questions=[str(x) for x in data.get("questions", [])][:3],
        confidence_note=str(data.get("confidence_note", "")),
        model=MODEL,
    )
