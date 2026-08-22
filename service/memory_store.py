"""Персистентная память сервиса: справки о машинах и подтверждённые исправления.

SQLite-файл в CORPUS_DIR — том же смонтированном томе, что уже переживает
пересборку образа (см. docker-compose.yml). Никакой новой инфраструктуры:
stdlib sqlite3, ноль новых зависимостей.

Раньше справки жили в process-local словаре в vehicle_facts.py и обнулялись
на каждом редеплое. Таблица vehicle_facts здесь — тот же кэш, но на диске.

Ради чего это на самом деле нужно — таблица corrections. Когда владелец
подтверждает через /api/feedback, что реально нашли в сервисе, это не только
цикл доверия «сервис учится». label_mapper.py размечает свободный текст
владельца одним из 21 канонического класса cause (тем же, что в
labels_ru.PARTS — вокабуляр снят прямо с models/best_model_clap.joblib), и в
этом виде запись готова к `cardiag ingest --cause <класс>` — апстримному пути
приёма "bring your own audio". service/export_training_set.py собирает такие
записи в обучающий корпус.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicle_facts (
    key TEXT PRIMARY KEY,
    brand TEXT,
    model TEXT,
    year TEXT,
    engine_spec TEXT,
    facts_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rec_id TEXT NOT NULL,
    vehicle_key TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    year TEXT,
    mileage TEXT,
    engine_spec TEXT,
    symptom TEXT,
    ai_status TEXT,
    ai_zone TEXT,
    ai_top_part TEXT,
    owner_text TEXT NOT NULL,
    comment TEXT,
    mapped_label TEXT,
    mapped_confidence TEXT,
    audio_path TEXT,
    consent_training INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    exported_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_corrections_export
    ON corrections (consent_training, mapped_label, exported_at);
"""

# sqlite3.Connection нельзя расшаривать между потоками без serialize — проще
# держать один процесс-wide лок, чем городить пул. Локальный файл, операции
# микросекундные, конкуренция не станет узким местом раньше, чем понадобится
# PostgreSQL по совсем другим причинам.
_lock = threading.Lock()


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # --- справки о машинах --------------------------------------------

    def get_facts(self, key: str) -> dict | None:
        if not key:
            return None
        with _lock, self._connect() as conn:
            row = conn.execute(
                "SELECT facts_json FROM vehicle_facts WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["facts_json"]) if row else None

    def save_facts(self, key: str, vehicle: dict, facts: dict) -> None:
        if not key or not facts:
            return
        with _lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO vehicle_facts
                   (key, brand, model, year, engine_spec, facts_json, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET
                     facts_json = excluded.facts_json,
                     fetched_at = excluded.fetched_at""",
                (key, vehicle.get("brand", ""), vehicle.get("model", ""),
                 vehicle.get("year", ""), vehicle.get("engine_spec", ""),
                 json.dumps(facts, ensure_ascii=False)),
            )

    # --- подтверждённые исправления ------------------------------------

    def save_correction(self, *, rec_id: str, vehicle_key: str, vehicle: dict,
                        symptom: str, ai_status: str, ai_zone: str,
                        ai_top_part: str, owner_text: str, comment: str,
                        audio_path: str, consent_training: bool) -> int:
        """Записать подтверждение и вернуть id строки — по нему потом
        проставится mapped_label из фонового классификатора."""
        with _lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO corrections
                   (rec_id, vehicle_key, brand, model, year, mileage, engine_spec,
                    symptom, ai_status, ai_zone, ai_top_part, owner_text, comment,
                    audio_path, consent_training)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rec_id, vehicle_key, vehicle.get("brand", ""), vehicle.get("model", ""),
                 vehicle.get("year", ""), vehicle.get("mileage", ""),
                 vehicle.get("engine_spec", ""), symptom, ai_status, ai_zone,
                 ai_top_part, owner_text, comment, audio_path, int(consent_training)),
            )
            return cur.lastrowid

    def set_mapped_label(self, correction_id: int, label: str | None,
                         confidence: str) -> None:
        with _lock, self._connect() as conn:
            conn.execute(
                "UPDATE corrections SET mapped_label = ?, mapped_confidence = ? "
                "WHERE id = ?", (label, confidence, correction_id))

    def export_candidates(self) -> list[dict]:
        """Строки, готовые лечь в обучающий корпус: подтверждены, размечены,
        владелец дал согласие, ещё не экспортированы."""
        with _lock, self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM corrections
                   WHERE consent_training = 1
                     AND mapped_label IS NOT NULL
                     AND exported_at IS NULL
                   ORDER BY mapped_label""").fetchall()
        return [dict(r) for r in rows]

    def mark_exported(self, ids: list[int]) -> None:
        if not ids:
            return
        with _lock, self._connect() as conn:
            conn.executemany(
                "UPDATE corrections SET exported_at = datetime('now') WHERE id = ?",
                [(i,) for i in ids])

    def stats(self) -> dict:
        with _lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM corrections").fetchone()["c"]
            mapped = conn.execute(
                "SELECT COUNT(*) c FROM corrections WHERE mapped_label IS NOT NULL"
            ).fetchone()["c"]
            ready = conn.execute(
                "SELECT COUNT(*) c FROM corrections WHERE consent_training = 1 "
                "AND mapped_label IS NOT NULL AND exported_at IS NULL"
            ).fetchone()["c"]
            exported = conn.execute(
                "SELECT COUNT(*) c FROM corrections WHERE exported_at IS NOT NULL"
            ).fetchone()["c"]
            by_label = conn.execute(
                "SELECT mapped_label, COUNT(*) c FROM corrections "
                "WHERE mapped_label IS NOT NULL GROUP BY mapped_label "
                "ORDER BY c DESC").fetchall()
            facts_cached = conn.execute(
                "SELECT COUNT(*) c FROM vehicle_facts").fetchone()["c"]
        return {
            "corrections_total": total,
            "corrections_mapped": mapped,
            "ready_to_export": ready,
            "already_exported": exported,
            "by_label": {r["mapped_label"]: r["c"] for r in by_label},
            "vehicle_facts_cached": facts_cached,
        }
