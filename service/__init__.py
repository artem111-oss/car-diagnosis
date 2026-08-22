"""Продуктовый слой поверх cardiag: русский интерфейс, согласование голов,
разбор механиком и сбор корпуса.

Namespace не пересекается с src/cardiag, и ни один файл апстрима отсюда не
правится — обновления подтягиваются без конфликтов.

Здесь же читается .env. Загрузка идёт при импорте пакета, до того как
service.mechanic прочитает свои настройки на уровне модуля. Отдельная
зависимость не нужна: формат простой, а лишний пакет в проде — лишний риск.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> None:
    """Подтянуть KEY=VALUE из .env, не затирая уже заданное окружение.

    Переменные окружения важнее файла: в проде значения приходят из docker
    compose или systemd, и .env не должен их перебивать.
    """
    env = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    if not env.is_file():
        return
    # utf-8-sig, а не utf-8: редакторы и PowerShell на Windows пишут BOM, из-за
    # которого первый ключ читается как "﻿AIMLAPI_KEY" и молча теряется.
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()
