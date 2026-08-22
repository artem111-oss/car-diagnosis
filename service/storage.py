"""Хранилище записей: это не логи, а будущий обучающий корпус.

Ценность сервиса не в коде — код открыт по MIT и его повторит любой. Ценность в
парах «аудио с российской машины + подтверждённый мастером диагноз». Поэтому
каждая запись кладётся рядом с метаданными в формате, из которого потом
собирается датасет для переобучения голов.

Раскладка на диске повторяет структуру объектного хранилища, чтобы переезд на
S3 свёлся к замене двух функций:

    corpus/
      audio/<id>.wav
      records.jsonl        одна строка на анализ
      feedback.jsonl       одна строка на подтверждение от сервиса
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


class Corpus:
    def __init__(self, root: str | Path = "corpus"):
        self.root = Path(root)
        self.audio_dir = self.root / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.records = self.root / "records.jsonl"
        self.feedback = self.root / "feedback.jsonl"

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def audio_path(self, rec_id: str) -> Path:
        return self.audio_dir / f"{rec_id}.wav"

    def save_record(self, rec_id: str, *, vehicle: dict, symptom: str,
                    report: dict, consent_training: bool) -> None:
        """Записать результат анализа.

        consent_training хранится вместе с записью: без явного согласия
        пользователя аудио нельзя использовать для дообучения (152-ФЗ).
        """
        row = {
            "id": rec_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "vehicle": vehicle,
            "symptom": symptom,
            "consent_training": bool(consent_training),
            "status": report.get("status"),
            "zone": report.get("zone"),
            "band": report.get("band"),
            "conflict": report.get("conflict"),
            "clean_seconds": report.get("clean_seconds"),
            "top_part": (report.get("versions") or [{}])[0].get("part", ""),
            "debug": report.get("debug", {}),
        }
        self._append(self.records, row)

    def save_feedback(self, rec_id: str, *, actual: str, comment: str) -> None:
        """Что на самом деле нашли на подъёмнике.

        Это и есть замыкание цикла: пара «аудио → подтверждённая причина»
        отправляется в приоритетную очередь на разметку. Жалоба «диагноз не
        подтвердился» тоже приходит сюда и становится обучающим примером.
        """
        self._append(self.feedback, {
            "id": rec_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actual": actual,
            "comment": comment,
        })

    def stats(self) -> dict:
        """Сводка для оператора: сколько собрано и сколько подтверждено."""
        records = self._read(self.records)
        feedback = self._read(self.feedback)
        confirmed = {f["id"] for f in feedback}

        agreed = 0
        for r in records:
            if r["id"] in confirmed:
                fb = next(f for f in feedback if f["id"] == r["id"])
                if fb.get("actual") and fb["actual"] == r.get("top_part"):
                    agreed += 1

        by_status: dict[str, int] = {}
        for r in records:
            by_status[r.get("status", "?")] = by_status.get(r.get("status", "?"), 0) + 1

        return {
            "records": len(records),
            "with_feedback": len(confirmed),
            "matched_top_part": agreed,
            # Единственная честная метрика качества в проде. Пока подтверждений
            # мало, показываем сырые числа, а не проценты.
            "by_status": by_status,
            "conflicts": sum(1 for r in records if r.get("conflict")),
            "trainable": sum(1 for r in records if r.get("consent_training")),
        }

    @staticmethod
    def _append(path: Path, row: dict) -> None:
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        out = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
