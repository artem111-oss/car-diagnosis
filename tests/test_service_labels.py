"""Проверки продуктового слоя, не требующие модели и сети.

Главный тест здесь — полнота словаря: если апстрим переобучит модель с новыми
метками, сервис не должен молча показать англоязычный класс владельцу машины.
"""
from __future__ import annotations

from pathlib import Path

import joblib
import pytest

from service.labels_ru import PARTS, REGIONS, part_ru, region_ru, zones_agree

MODELS = Path(__file__).resolve().parents[1] / "models"


def _model_classes(head: str) -> list[str]:
    obj = joblib.load(MODELS / "best_model_clap.joblib")
    return [str(c) for c in obj["heads"][head].classes_]


def test_every_cause_class_has_a_russian_card():
    """Каждый класс cause переведён. Ловит дрейф словаря при обновлении модели."""
    missing = [c for c in _model_classes("cause") if c not in PARTS]
    assert not missing, f"нет русской карточки для: {missing}"


def test_every_region_class_has_a_russian_name():
    missing = [z for z in _model_classes("region") if z not in REGIONS]
    assert not missing, f"нет русского названия зоны: {missing}"


def test_every_part_points_at_a_real_zone():
    """Согласование сломается молча, если деталь ссылается на несуществующую зону."""
    bad = {p: info.zone for p, info in PARTS.items() if info.zone not in REGIONS}
    assert not bad, f"детали ссылаются на неизвестные зоны: {bad}"


def test_typo_alias_matches_the_correct_label():
    """bad_wheal_bearing — опечатка в обучающем корпусе, узел тот же."""
    assert part_ru("bad_wheal_bearing").name == part_ru("wheel_bearing").name
    assert part_ru("bad_wheal_bearing").zone == part_ru("wheel_bearing").zone


@pytest.mark.parametrize("zone,part,expected", [
    ("engine", "valvetrain", True),
    ("engine", "rod_knock", True),
    ("brakes/wheels", "brakes", True),
    ("drivetrain", "cv_axle", True),
    # Ровно тот случай, что модель выдаёт на demo.wav: зона и деталь несовместимы.
    ("suspension/steering", "exhaust", False),
    ("engine", "brakes", False),
    ("brakes/wheels", "valvetrain", False),
])
def test_zone_part_compatibility(zone, part, expected):
    assert zones_agree(zone, part) is expected


def test_unknown_labels_do_not_crash():
    """Неизвестная метка не должна ронять ответ пользователю."""
    info = part_ru("something_new_upstream_added")
    assert info.name
    assert zones_agree("engine", "something_new_upstream_added") is False
    assert region_ru("unknown_zone") == "unknown_zone"
