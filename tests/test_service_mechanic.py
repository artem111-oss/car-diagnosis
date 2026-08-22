"""Тесты слоя механика без обращения к сети.

Главное требование к нему — не ломать основной ответ. Акустический вердикт уже
посчитан к моменту вызова, и любая проблема с LLM должна деградировать в пустой
блок, а не в ошибку пользователю.
"""
from __future__ import annotations

import pytest

from service import mechanic


@pytest.fixture
def report():
    return {
        "status": "fault",
        "band": "low",
        "band_gloss": "Это склонность, а не вывод.",
        "clean_seconds": 5.8,
        "total_seconds": 10.1,
        "conflict": False,
        "debug": {
            "knock_probability": 0.015,
            "regions": [{"zone": "engine", "p": 0.975}],
            "causes": [{"part": "valvetrain", "p": 0.983}],
        },
    }


def test_no_key_degrades_quietly(report, monkeypatch):
    monkeypatch.delenv("AIMLAPI_KEY", raising=False)
    op = mechanic.ask(report, {"brand": "Lada"}, "цокот")
    assert op.ok is False
    assert op.error
    assert op.to_dict()["parts"] == []


def test_uncertain_verdict_skips_the_call(report, monkeypatch):
    """Рассуждать не о чем: модель начнёт выдумывать, а мы заплатим за токены."""
    monkeypatch.setenv("AIMLAPI_KEY", "test-key")
    report["status"] = "uncertain"

    def explode(*a, **k):
        raise AssertionError("сеть не должна дёргаться на uncertain")

    monkeypatch.setattr(mechanic.httpx, "post", explode)
    assert mechanic.ask(report, {}, "").ok is False


def test_prompt_carries_the_evidence(report):
    p = mechanic._prompt(report, {"brand": "Lada", "model": "Granta",
                                  "mileage": "150000"}, "цокот на холостых")
    assert "engine" in p and "valvetrain" in p
    assert "Lada Granta" in p and "150000" in p
    assert "цокот на холостых" in p


def test_prompt_flags_head_conflict(report):
    """При расхождении зоны и детали модель должна знать, что детали верить нельзя."""
    report["conflict"] = True
    assert "противоречат" in mechanic._prompt(report, {}, "")


@pytest.mark.parametrize("raw", [
    '{"diagnosis":"цокот"}',
    '```json\n{"diagnosis":"цокот"}\n```',
    '```\n{"diagnosis":"цокот"}\n```',
    'Вот результат:\n{"diagnosis":"цокот"}\nГотово.',
])
def test_parse_survives_markdown_wrapping(raw):
    """Модели оборачивают JSON в markdown вопреки инструкции."""
    assert mechanic._parse(raw)["diagnosis"] == "цокот"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        mechanic._parse("никакого JSON тут нет")


def test_http_failure_does_not_raise(report, monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "test-key")

    class Boom:
        status_code = 500
        text = "upstream down"

    monkeypatch.setattr(mechanic.httpx, "post", lambda *a, **k: Boom())
    op = mechanic.ask(report, {}, "")
    assert op.ok is False and "500" in op.error


def test_timeout_does_not_raise(report, monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "test-key")

    def timeout(*a, **k):
        raise mechanic.httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(mechanic.httpx, "post", timeout)
    assert mechanic.ask(report, {}, "").ok is False


def test_vehicle_line_handles_missing_fields():
    assert mechanic._vehicle_line({}) == "марка не указана"
    assert "Lada" in mechanic._vehicle_line({"brand": "Lada"})


def test_vehicle_line_carries_engine_spec_to_the_mechanic():
    line = mechanic._vehicle_line({"brand": "Lada", "model": "Granta",
                                   "engine_spec": "1.6, 8 клапанов"})
    assert "1.6, 8 клапанов" in line


def test_refine_keeps_the_conversation(monkeypatch):
    """Уточнение продолжает диалог, а не начинает новый: механик должен
    помнить, что уже предполагал, иначе вычёркивать будет нечего."""
    monkeypatch.setenv("AIMLAPI_KEY", "test-key")
    monkeypatch.setattr(mechanic, "_call", lambda msgs: (
        mechanic.MechanicOpinion(ok=True, diagnosis="направляющие",
                                 ruled_out=["гидрокомпенсаторы"]), '{"x":1}'))

    prior = [{"role": "system", "content": "s"},
             {"role": "user", "content": "u"},
             {"role": "assistant", "content": "a"}]
    op, convo = mechanic.refine(prior, "зазоры в норме")

    assert op.ok and op.ruled_out == ["гидрокомпенсаторы"]
    assert convo[:3] == prior, "прежняя история должна сохраниться"
    assert "зазоры в норме" in convo[3]["content"]
    assert convo[-1]["role"] == "assistant"


def test_refine_tells_the_model_not_to_defend_ruled_out_causes(monkeypatch):
    """Наблюдалось вживую: исключённую версию модель защищала аргументом
    «та же причина, просто по другой механике». Правило про переход к редким
    причинам должно уходить в запрос."""
    monkeypatch.setenv("AIMLAPI_KEY", "test-key")
    captured = {}

    def grab(msgs):
        captured["text"] = msgs[-1]["content"]
        return mechanic.MechanicOpinion(ok=True), "{}"

    monkeypatch.setattr(mechanic, "_call", grab)
    mechanic.refine([{"role": "user", "content": "u"}], "проверял, в норме")

    assert "ОТПАДАЕТ" in captured["text"]
    assert "редким" in captured["text"]


def test_refine_without_key_returns_history_untouched(monkeypatch):
    monkeypatch.delenv("AIMLAPI_KEY", raising=False)
    prior = [{"role": "user", "content": "u"}]
    op, convo = mechanic.refine(prior, "ответ")
    assert op.ok is False and convo == prior
