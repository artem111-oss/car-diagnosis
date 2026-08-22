"""Справочник по автомобилю: кэш, деградация и попадание фактов в промпт.

Появился после живой ошибки: на Lada Granta (мотор 11183, ремень ГРМ) модель
предположила растянутую цепь и построила на ней рассуждение про датчик
распредвала. Узла, которого в машине нет, достаточно, чтобы весь разбор стал
мусором.
"""
from __future__ import annotations

import pytest

from service import mechanic, vehicle_facts

FACTS = {
    "engines": "1.6 8V ВАЗ-11183 и 1.6 16V ВАЗ-21126",
    "timing_drive": "ремень ГРМ на всех штатных двигателях",
    "hydraulic_lifters": "на 8-клапанных гидрокомпенсаторов нет",
    "known_issues": ["расход масла к 150 тыс. км"],
    "not_applicable": ["Цепной привод ГРМ", "Дизельный двигатель"],
    "confidence": "medium",
}


@pytest.fixture(autouse=True)
def clean_cache():
    vehicle_facts._cache.clear()
    yield
    vehicle_facts._cache.clear()


def test_lookup_is_cached_by_vehicle(monkeypatch):
    """Массовых машин немного: без кэша один и тот же запрос оплачивался бы
    на каждом анализе."""
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    calls = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            calls.append(1)
            return {"choices": [{"message": {"content": '{"timing_drive":"ремень"}'}}]}

    monkeypatch.setattr(vehicle_facts.httpx, "post", lambda *a, **k: Resp())

    veh = {"brand": "Lada", "model": "Granta", "year": "2015"}
    vehicle_facts.lookup(veh)
    vehicle_facts.lookup(veh)
    vehicle_facts.lookup({"brand": "LADA", "model": "granta ", "year": "2015"})

    assert len(calls) == 1, "разный регистр и пробелы — это та же машина"


def test_different_engines_on_same_model_are_not_the_same_cache_entry(monkeypatch):
    """Granta 8V и 16V — разный привод ГРМ и разный клапанной механизм на
    одной модели. Без engine_spec в ключе вторая машина получила бы справку,
    выуженную для первой."""
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, engine):
            self.engine = engine

        def json(self):
            calls.append(self.engine)
            return {"choices": [{"message": {"content": '{"timing_drive":"x"}'}}]}

    def fake_post(url, *, json, **kw):
        return Resp(json["messages"][0]["content"])

    monkeypatch.setattr(vehicle_facts.httpx, "post", fake_post)

    vehicle_facts.lookup({"brand": "Lada", "model": "Granta", "year": "2015",
                          "engine_spec": "1.6, 8 клапанов"})
    vehicle_facts.lookup({"brand": "Lada", "model": "Granta", "year": "2015",
                          "engine_spec": "1.6, 16 клапанов"})
    vehicle_facts.lookup({"brand": "Lada", "model": "Granta", "year": "2015"})

    assert len(calls) == 3, "разный двигатель — разный запрос, разная запись в кэше"


def test_engine_spec_reaches_the_sonar_prompt(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    seen = {}

    def fake_post(url, *, json, **kw):
        seen["content"] = json["messages"][0]["content"]

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "{}"}}]}
        return R()

    monkeypatch.setattr(vehicle_facts.httpx, "post", fake_post)
    vehicle_facts.lookup({"brand": "Lada", "model": "Granta", "year": "2015",
                          "engine_spec": "1.6, 8 клапанов"})
    assert "1.6, 8 клапанов" in seen["content"]


def test_lookup_without_brand_skips_the_call(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")

    def explode(*a, **k):
        raise AssertionError("без марки спрашивать нечего")

    monkeypatch.setattr(vehicle_facts.httpx, "post", explode)
    assert vehicle_facts.lookup({"model": "Granta"}) == {}


def test_lookup_survives_upstream_failure(monkeypatch):
    """Разбор должен состояться и без справки — просто с оговоркой."""
    monkeypatch.setenv("AIMLAPI_KEY", "k")

    def boom(*a, **k):
        raise vehicle_facts.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(vehicle_facts.httpx, "post", boom)
    assert vehicle_facts.lookup({"brand": "Lada"}) == {}


def test_prompt_block_carries_the_forbidden_parts():
    block = vehicle_facts.as_prompt_block(FACTS)
    assert "ремень ГРМ" in block
    assert "Цепной привод ГРМ" in block
    assert "НЕТ" in block, "запрет должен читаться как запрет, а не как справка"


def test_prompt_block_warns_when_facts_are_missing():
    block = vehicle_facts.as_prompt_block({})
    assert "не утверждай" in block.lower()


def test_facts_reach_the_mechanic_prompt():
    report = {"status": "fault", "band": "low", "debug": {
        "regions": [{"zone": "engine", "p": 0.9}], "causes": [], "knock_probability": 0.0}}
    p = mechanic._prompt(report, {"brand": "Lada", "model": "Granta"},
                         "цокот", FACTS)
    assert "ремень ГРМ" in p
    assert "Цепной привод ГРМ" in p


def test_system_prompt_forbids_inventing_parts():
    assert "которого на этой машине нет" in mechanic.SYSTEM
    assert "ЗОНУ" in mechanic.SYSTEM, "сверка с акустикой должна быть в правилах"


def test_refine_asks_the_model_to_self_check(monkeypatch):
    """Срыв произошёл именно на уточнении, где разговор дрейфует."""
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    seen = {}

    def grab(msgs):
        seen["text"] = msgs[-1]["content"]
        return mechanic.MechanicOpinion(ok=True), "{}"

    monkeypatch.setattr(mechanic, "_call", grab)
    mechanic.refine([{"role": "user", "content": "u"}], "проверил")

    assert "сверься сам с собой" in seen["text"].lower()
    assert "ремень ГРМ" in seen["text"], "напоминание про выдуманные детали"
