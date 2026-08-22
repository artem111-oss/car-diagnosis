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

from service import vehicle_facts

log = logging.getLogger(__name__)

API_URL = os.getenv("AIMLAPI_URL", "https://api.aimlapi.com/v1/chat/completions")
MODEL = os.getenv("AIMLAPI_MODEL", "anthropic/claude-haiku-latest")
TIMEOUT = float(os.getenv("AIMLAPI_TIMEOUT", "60"))
# 1600 не хватало: уточнение длиннее первого разбора на поле ruled_out, ответ
# обрывался по лимиту и JSON приходил битым всегда в одном месте.
MAX_TOKENS = int(os.getenv("AIMLAPI_MAX_TOKENS", "3000"))


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
0. НИКОГДА не называй узел, которого на этой машине нет. Если в справке сказано
   «ремень ГРМ» — про цепь речи быть не может, и наоборот. Не знаешь
   устройство — не утверждай, а спроси у владельца. Одна выдуманная деталь
   обесценивает весь разбор.
1. Каждая версия должна объяснять ИМЕННО ТУ ЗОНУ, которую услышала акустика.
   Если предлагаешь узел из другой зоны — прямо скажи, что расходишься с
   акустикой, и объясни почему. Молча уходить в сторону нельзя.
2. Не выдумывай уверенность. Если улик мало, скажи об этом прямо.
3. Опирайся на связь симптома с механикой. Объясняй, почему звук ведёт себя
   именно так (меняется с оборотами, с нагрузкой, с температурой).
4. Цены давай в рублях, реалистичные для России, с учётом работы.
5. Если подозреваешь узел безопасности (тормоза, рулевое, подвеска) — ставь
   высокую срочность.
6. Пиши для владельца машины, а не для механика. Термин используй, только если
   без него теряется смысл, и поясняй его.
7. НЕ приводи числовые вероятности в тексте для пользователя. Цифры вроде
   "0.924" тебе даны как рабочая информация, но они не откалиброваны, и мы
   намеренно не показываем их владельцу. Говори словами: "звук характерен для",
   "менее вероятно", "почти исключено".
8. Задай 1-3 уточняющих вопроса, ответы на которые сузили бы поиск.
9. Возвращай ТОЛЬКО JSON по схеме, без markdown и пояснений вокруг."""

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
    # Версии, отпавшие после ответов владельца. Показать, что именно исключено,
    # часто полезнее нового списка догадок.
    ruled_out: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _vehicle_line(v: dict) -> str:
    bits = [v.get("brand", ""), v.get("model", ""), v.get("year", "")]
    name = " ".join(b for b in bits if b).strip() or "марка не указана"
    engine_spec = v.get("engine_spec", "").strip()
    if engine_spec:
        name += f", двигатель: {engine_spec}"
    km = v.get("mileage", "")
    return f"{name}, пробег {km} км" if km else name


def _prompt(report: dict, vehicle: dict, symptom: str, facts: dict | None = None) -> str:
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
        vehicle_facts.as_prompt_block(facts or {}),
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


def _to_opinion(data: dict) -> MechanicOpinion:
    return MechanicOpinion(
        ok=True,
        diagnosis=str(data.get("diagnosis", "")),
        reasoning=str(data.get("reasoning", "")),
        parts=[p for p in data.get("parts", []) if isinstance(p, dict)][:5],
        urgency=str(data.get("urgency", "")),
        urgency_why=str(data.get("urgency_why", "")),
        diy_checks=[str(x) for x in data.get("diy_checks", [])][:5],
        questions=[str(x) for x in data.get("questions", [])][:3],
        confidence_note=str(data.get("confidence_note", "")),
        ruled_out=[str(x) for x in data.get("ruled_out", [])][:5],
        model=MODEL,
    )


def _once(messages: list[dict], key: str) -> tuple[MechanicOpinion, str]:
    try:
        r = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={
                "model": MODEL,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": MAX_TOKENS,
                # Просить JSON в промпте недостаточно: на длинных ответах модель
                # срывается в невалидный синтаксис. Наблюдалось на уточнении —
                # "Expecting ',' delimiter: line 25".
                "response_format": {"type": "json_object"},
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError as e:
        log.warning("AIMLAPI недоступен: %s", e)
        return MechanicOpinion(error="сервис разбора временно недоступен"), ""

    if r.status_code != 200:
        log.warning("AIMLAPI %s: %s", r.status_code, r.text[:200])
        return MechanicOpinion(error=f"сервис разбора вернул {r.status_code}"), ""

    try:
        choice = r.json()["choices"][0]
        raw = choice["message"]["content"]
        finish = choice.get("finish_reason", "")
    except (KeyError, ValueError) as e:
        log.warning("неожиданная форма ответа AIMLAPI: %s", e)
        return MechanicOpinion(error="не удалось разобрать ответ"), ""

    try:
        return _to_opinion(_parse(raw)), raw
    except (ValueError, json.JSONDecodeError) as e:
        # Обрыв по лимиту токенов даёт синтаксически битый JSON всегда в одном
        # месте, и повтор его не лечит — нужен запас по max_tokens.
        if finish == "length":
            log.warning("ответ обрезан по max_tokens=%d, JSON неполный", MAX_TOKENS)
            return MechanicOpinion(error="ответ не поместился в лимит"), ""
        log.warning("не разобрали ответ LLM (finish_reason=%s): %s", finish, e)
        return MechanicOpinion(error="не удалось разобрать ответ"), ""


def _call(messages: list[dict]) -> tuple[MechanicOpinion, str]:
    """Заход к модели с одним повтором на случай битого JSON.

    Никогда не бросает: акустический вердикт уже у пользователя на экране, и
    проблема с LLM должна деградировать в пустой блок, а не в ошибку.
    """
    key = api_key()
    if not key:
        return MechanicOpinion(error="AIMLAPI_KEY не задан"), ""

    opinion, raw = _once(messages, key)
    if opinion.ok or opinion.error != "не удалось разобрать ответ":
        return opinion, raw

    # Повторяем только разбор ответа: сеть и коды ошибок повтором не лечатся,
    # а вот сорванный синтаксис со второй попытки обычно выходит корректным.
    log.info("повторяю запрос к модели после битого JSON")
    return _once(messages, key)


def build_messages(report: dict, vehicle: dict, symptom: str,
                   facts: dict | None = None) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": _prompt(report, vehicle, symptom, facts)}]


def ask(report: dict, vehicle: dict, symptom: str) -> MechanicOpinion:
    """Первый разбор по акустике, марке и симптому."""
    # На неуверенном вердикте рассуждать не о чем: модель начнёт придумывать,
    # а мы заплатим за токены. Экономит деньги и убирает источник выдумок.
    if report.get("status") == "uncertain":
        return MechanicOpinion(error="акустика не дала опоры для разбора")
    if not api_key():
        return MechanicOpinion(error="AIMLAPI_KEY не задан")
    facts = vehicle_facts.lookup(vehicle)
    return _call(build_messages(report, vehicle, symptom, facts))[0]


def refine(messages: list[dict], answers: str) -> tuple[MechanicOpinion, list[dict]]:
    """Уточнить вывод по ответам владельца.

    Здесь механик впервые получает то, чего нет ни в звуке, ни в анкете: что
    уже проверяли и с каким результатом. На реальном случае это решает исход —
    если зазоры клапанов замерены и в норме, версия про гидрокомпенсаторы
    отпадает, и на первое место выходят направляющие. Без ответа владельца
    модель до этого не дойдёт никогда.

    Возвращает мнение и продолженную историю, чтобы диалог можно было вести
    дальше.
    """
    if not api_key():
        return MechanicOpinion(error="AIMLAPI_KEY не задан"), messages

    convo = messages + [{"role": "user", "content": (
        "Владелец ответил на твои вопросы и добавил подробности:\n\n"
        f"{answers}\n\n"
        "Уточни вывод с учётом этого.\n\n"
        "Главное правило: если ответ владельца исключает версию — она "
        "ОТПАДАЕТ, и держаться за неё нельзя. Не выкручивайся аргументом "
        "вида «типовая причина всё равно возможна, просто по другой "
        "механике». Когда частые причины отпали, переходи к более редким, "
        "которые объясняют ровно эту картину: износ направляющих и стержней "
        "клапанов, износ постели или кулачков распредвала, задиры, ослабший "
        "натяжитель, дефект конкретной детали. Проверенное владельцем — это "
        "факт, а не мнение.\n\n"
        "Перед ответом сверься сам с собой:\n"
        "1) Есть ли предлагаемый узел на ЭТОЙ машине? Сверься с проверенными "
        "данными выше. Если там сказано «ремень ГРМ» — про цепь писать нельзя, "
        "и наоборот. Выдуманная деталь обесценивает весь разбор.\n"
        "2) Объясняет ли версия ту зону, которую услышала акустика? Если "
        "уходишь в другую зону — скажи об этом прямо.\n"
        "3) Не противоречит ли вывод тому, что владелец уже проверил?\n\n"
        "Перечисли отпавшие версии в поле \"ruled_out\" и подними на первое "
        "место ту, что теперь вероятнее. Если данных всё ещё не хватает, "
        "задай новые вопросы. Формат тот же JSON, плюс поле \"ruled_out\": "
        "[\"версия, которая отпала, и почему\"]."
    )}]

    opinion, raw = _call(convo)
    if opinion.ok and raw:
        convo = convo + [{"role": "assistant", "content": raw}]
    return opinion, convo
