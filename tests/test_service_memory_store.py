"""SQLite-хранилище справок и подтверждённых исправлений. Ни сети, ни LLM —
только диск, поэтому тесты быстрые и детерминированные.
"""
from __future__ import annotations

import pytest

from service.memory_store import MemoryStore

VEHICLE = {"brand": "Lada", "model": "Granta", "year": "2015", "mileage": "150000",
          "engine_spec": "1.6, 8 клапанов"}
FACTS = {"timing_drive": "ремень ГРМ", "confidence": "medium"}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory.db")


def test_facts_round_trip(store):
    key = "lada granta 2015"
    assert store.get_facts(key) is None

    store.save_facts(key, VEHICLE, FACTS)
    assert store.get_facts(key) == FACTS


def test_facts_survive_a_fresh_connection(tmp_path):
    """Переживает пересборку образа — здесь это переживает новый инстанс,
    указывающий на тот же файл."""
    db = tmp_path / "memory.db"
    MemoryStore(db).save_facts("kia rio 2014", VEHICLE, FACTS)
    assert MemoryStore(db).get_facts("kia rio 2014") == FACTS


def test_facts_upsert_overwrites(store):
    key = "lada granta 2015"
    store.save_facts(key, VEHICLE, FACTS)
    store.save_facts(key, VEHICLE, {"timing_drive": "обновлено"})
    assert store.get_facts(key) == {"timing_drive": "обновлено"}


def test_engine_spec_persists_in_corrections(store):
    """Разные моторы одной модели дают разный привод ГРМ — эта колонка нужна,
    чтобы потом можно было отличить один случай Granta от другого при разборе
    качества обучающего корпуса."""
    cid = store.save_correction(
        rec_id="abc", vehicle_key="lada granta 2015 1.6, 8 клапанов",
        vehicle=VEHICLE, symptom="цокот", ai_status="fault", ai_zone="Двигатель",
        ai_top_part="valvetrain", owner_text="направляющие клапанов",
        comment="", audio_path="/x.wav", consent_training=True,
    )
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT engine_spec FROM corrections WHERE id = ?",
                         (cid,)).fetchone()
    assert r["engine_spec"] == "1.6, 8 клапанов"


def test_save_correction_returns_id(store):
    cid = store.save_correction(
        rec_id="abc123", vehicle_key="lada granta 2015", vehicle=VEHICLE,
        symptom="цокот", ai_status="fault", ai_zone="Двигатель",
        ai_top_part="valvetrain", owner_text="направляющие клапанов",
        comment="", audio_path="/data/abc123.wav", consent_training=True,
    )
    assert isinstance(cid, int) and cid > 0


def test_export_candidates_require_mapped_label(store):
    cid = store.save_correction(
        rec_id="abc", vehicle_key="lada granta 2015", vehicle=VEHICLE,
        symptom="", ai_status="fault", ai_zone="", ai_top_part="",
        owner_text="направляющие", comment="", audio_path="/x.wav",
        consent_training=True,
    )
    assert store.export_candidates() == [], "без метки экспортировать нечего"

    store.set_mapped_label(cid, "valvetrain", "high")
    candidates = store.export_candidates()
    assert len(candidates) == 1
    assert candidates[0]["mapped_label"] == "valvetrain"


def test_export_candidates_require_consent(store):
    cid = store.save_correction(
        rec_id="abc", vehicle_key="k", vehicle=VEHICLE, symptom="", ai_status="",
        ai_zone="", ai_top_part="", owner_text="что-то", comment="",
        audio_path="/x.wav", consent_training=False,
    )
    store.set_mapped_label(cid, "valvetrain", "high")
    assert store.export_candidates() == [], "без согласия на обучение — не экспортируем"


def test_mark_exported_makes_it_disappear_from_candidates(store):
    cid = store.save_correction(
        rec_id="abc", vehicle_key="k", vehicle=VEHICLE, symptom="", ai_status="",
        ai_zone="", ai_top_part="", owner_text="x", comment="",
        audio_path="/x.wav", consent_training=True,
    )
    store.set_mapped_label(cid, "valvetrain", "high")
    assert len(store.export_candidates()) == 1

    store.mark_exported([cid])
    assert store.export_candidates() == [], "экспортированное не предлагаем повторно"


def test_stats_reflect_pipeline_state(store):
    cid1 = store.save_correction(
        rec_id="a", vehicle_key="k", vehicle=VEHICLE, symptom="", ai_status="",
        ai_zone="", ai_top_part="", owner_text="x", comment="",
        audio_path="/x.wav", consent_training=True,
    )
    cid2 = store.save_correction(
        rec_id="b", vehicle_key="k", vehicle=VEHICLE, symptom="", ai_status="",
        ai_zone="", ai_top_part="", owner_text="y", comment="",
        audio_path="/y.wav", consent_training=True,
    )
    store.set_mapped_label(cid1, "valvetrain", "high")
    store.set_mapped_label(cid2, None, "low")  # классификатор не разобрался
    store.mark_exported([cid1])

    s = store.stats()
    assert s["corrections_total"] == 2
    assert s["corrections_mapped"] == 1  # cid2 размечен как None — не считается
    assert s["already_exported"] == 1
    assert s["ready_to_export"] == 0  # единственный размеченный уже экспортирован
    assert s["by_label"] == {"valvetrain": 1}


def test_mark_exported_handles_empty_list(store):
    store.mark_exported([])  # не должно бросать


def test_get_facts_empty_key_returns_none(store):
    assert store.get_facts("") is None
