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
