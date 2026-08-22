"""Собрать подтверждённые записи в обучающий корпус и передать апстриму.

Читает таблицу corrections: строки, где владелец подтвердил причину, дал
согласие на обучение при записи и уже размечены label_mapper.py одним из 21
канонического класса. Группирует WAV-файлы по классу и вызывает
cardiag.pipeline.build.ingest_dir() для каждой группы — тот же путь, которым
апстрим принимает "bring your own audio" (`cardiag ingest --cause <класс>`),
без изменений в src/cardiag.

Запуск:
    uv run python -m service.export_training_set
    uv run python -m service.export_training_set --db corpus/memory.db --min-total 150

Дальше вручную, не отсюда — переобучение не должно быть побочным эффектом
экспорта:
    cardiag train
    python -m cardiag.training.eval.scorecard   # сверить, что не просело
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from service.memory_store import MemoryStore

# ingest_dir молча проглотит и меньше, но переобучение на 1-2 клипах на класс
# добавляет шум, а не сигнал — дешевле подождать, чем потом гадать, откуда
# просела точность.
MIN_PER_CLASS = 5


def run(db_path: str, min_total: int) -> None:
    store = MemoryStore(db_path)
    rows = store.export_candidates()
    if not rows:
        print("Нечего экспортировать: нет подтверждённых и размеченных записей.")
        return

    if len(rows) < min_total:
        print(f"Всего {len(rows)} подтверждённых примеров — меньше порога "
              f"{min_total}. Дообучение на таком объёме скорее всего "
              f"переобучится на шуме, а не станет точнее. Продолжаю, но "
              f"результат стоит проверить особенно внимательно.\n")

    by_label: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_label[r["mapped_label"]].append(r)

    from cardiag.pipeline import build

    exported_ids: list[int] = []
    skipped_missing_audio = 0

    with tempfile.TemporaryDirectory() as tmp:
        for label, group in sorted(by_label.items()):
            if len(group) < MIN_PER_CLASS:
                print(f"  {label:16} {len(group):3} записей — пропускаю, "
                      f"меньше {MIN_PER_CLASS}")
                continue

            folder = Path(tmp) / label
            folder.mkdir()
            copied_ids = []
            for r in group:
                src = Path(r["audio_path"])
                if not src.is_file():
                    skipped_missing_audio += 1
                    continue
                shutil.copy(src, folder / f"{r['id']}.wav")
                copied_ids.append(r["id"])

            if not copied_ids:
                continue

            kind = "normal" if label == "normal" else "fault"
            cause = None if label == "normal" else label
            n = build.ingest_dir(str(folder), kind=kind, cause=cause, source="chtostuchit")
            print(f"  {label:16} {len(copied_ids):3} записей владельцев -> "
                  f"{n} обучающих клипов")
            exported_ids += copied_ids

    if skipped_missing_audio:
        print(f"\nПропущено {skipped_missing_audio} записей: WAV-файл не "
              f"найден на диске (не выживает ли corpus/ пересборку образа?).")

    if not exported_ids:
        print("\nНи одна запись не легла в корпус — вероятно, все меньше "
              f"{MIN_PER_CLASS} на класс или без аудио.")
        return

    store.mark_exported(exported_ids)
    print(f"\n{len(exported_ids)} записей вошли в корпус data/chtostuchit/. Дальше:")
    print("  cardiag train")
    print("  python -m cardiag.training.eval.scorecard")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default="corpus/memory.db")
    ap.add_argument("--min-total", type=int, default=150,
                    help="Порог для предупреждения, не жёсткая остановка.")
    args = ap.parse_args()
    run(args.db, args.min_total)
