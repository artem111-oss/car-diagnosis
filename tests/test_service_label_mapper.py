"""Классификатор свободного текста в канонический класс cause. Сеть замокана —
проверяем контракт и защиту от мусорных ответов LLM, а не реальную модель.
"""
from __future__ import annotations

import pytest

from service import label_mapper


def _fake_response(status_code=200, content='{"label":"valvetrain","confidence":"high"}'):
    class R:
        def __init__(self):
            self.status_code = status_code
            self.text = content

        def json(self):
            return {"choices": [{"message": {"content": content}}]}
    return R()


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("AIMLAPI_KEY", raising=False)
    label, conf = label_mapper.classify("направляющие клапанов")
    assert label is None and conf == "low"


def test_empty_text_skips_the_call(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")

    def explode(*a, **k):
        raise AssertionError("пустой текст классифицировать нечего")

    monkeypatch.setattr(label_mapper.httpx, "post", explode)
    assert label_mapper.classify("   ") == (None, "low")


def test_valid_canonical_label_passes_through(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post", lambda *a, **k: _fake_response())
    label, conf = label_mapper.classify("оказались направляющие клапанов")
    assert label == "valvetrain" and conf == "high"


def test_normal_sentinel_passes_through(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post", lambda *a, **k:
                        _fake_response(content='{"label":"normal","confidence":"medium"}'))
    label, _ = label_mapper.classify("оказалось что всё в порядке")
    assert label == "normal"


def test_hallucinated_label_is_rejected(monkeypatch):
    """LLM вернула класс не из списка — не пропускаем его в обучающий корпус
    молча, иначе ingest_dir получит --cause с несуществующим ключом."""
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post", lambda *a, **k:
                        _fake_response(content='{"label":"timing_chain","confidence":"high"}'))
    label, _ = label_mapper.classify("что-то с ремнём ГРМ")
    assert label is None


def test_null_label_from_model_passes_through(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post", lambda *a, **k:
                        _fake_response(content='{"label":null,"confidence":"low"}'))
    assert label_mapper.classify("мастер посмотрел и починил") == (None, "low")


def test_http_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post",
                        lambda *a, **k: _fake_response(status_code=500, content="boom"))
    assert label_mapper.classify("текст") == (None, "low")


def test_network_error_does_not_raise(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")

    def boom(*a, **k):
        raise label_mapper.httpx.ConnectTimeout("slow")

    monkeypatch.setattr(label_mapper.httpx, "post", boom)
    assert label_mapper.classify("текст") == (None, "low")


def test_garbage_json_does_not_raise(monkeypatch):
    monkeypatch.setenv("AIMLAPI_KEY", "k")
    monkeypatch.setattr(label_mapper.httpx, "post",
                        lambda *a, **k: _fake_response(content="это не JSON вообще"))
    assert label_mapper.classify("текст") == (None, "low")


def test_bad_wheal_bearing_typo_excluded_from_catalogue():
    """Опечатка апстрима не должна попасть в список классов, который видит
    классификатор — иначе он может её выбрать вместо wheel_bearing."""
    assert "bad_wheal_bearing" not in label_mapper._CANONICAL
    assert "wheel_bearing" in label_mapper._CANONICAL


def test_catalogue_matches_labels_ru():
    """Каждый класс из каталога реально существует в labels_ru.PARTS —
    ловит рассинхрон, если кто-то переименует ключ в одном месте и забудет
    про другое."""
    from service.labels_ru import PARTS
    for key in label_mapper._CANONICAL:
        assert key in PARTS
