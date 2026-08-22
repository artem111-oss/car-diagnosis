"""FastAPI-сервис: запись звука из браузера, анализ, сбор корпуса.

Запуск:
    uvicorn service.app:app --host 0.0.0.0 --port 8080

Модели грузятся один раз при старте: загрузка CLAP занимает около 9 секунд,
сам анализ — 0.23 секунды на CPU (замер на Windows 10, Python 3.11.16).
Поэтому GPU не нужен, но и грузить модель на каждый запрос нельзя.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from service.diagnose import Engine
from service.storage import Corpus

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_MB = 25

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["engine"] = Engine()
    _state["corpus"] = Corpus()
    yield
    _state.clear()


app = FastAPI(title="Диагностика по звуку", lifespan=lifespan)


def _to_wav(src: Path, dst: Path) -> None:
    """Привести что угодно из браузера к моно 48 кГц — родному формату CLAP.

    MediaRecorder отдаёт webm/opus или mp4, а не WAV, поэтому ffmpeg обязателен.
    """
    if not shutil.which("ffmpeg"):
        raise HTTPException(500, "На сервере нет ffmpeg — конвертация невозможна.")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "48000", "-ac", "1", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise HTTPException(400, "Не удалось прочитать аудио. Запишите ещё раз.")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/analyse")
async def analyse(
    audio: UploadFile,
    brand: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    mileage: str = Form(""),
    symptom: str = Form(""),
    consent_training: str = Form("false"),
):
    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "Пустой файл.")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {MAX_UPLOAD_MB} МБ.")

    corpus: Corpus = _state["corpus"]
    engine: Engine = _state["engine"]
    rec_id = corpus.new_id()

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "upload.bin"
        src.write_bytes(raw)
        wav = corpus.audio_path(rec_id)
        _to_wav(src, wav)

        try:
            report = engine.analyse(str(wav))
        except ValueError as e:
            raise HTTPException(400, f"Не удалось разобрать запись: {e}") from None

    payload = report.to_dict()
    payload["id"] = rec_id

    corpus.save_record(
        rec_id,
        vehicle={"brand": brand.strip(), "model": model.strip(),
                 "year": year.strip(), "mileage": mileage.strip()},
        symptom=symptom.strip(),
        report=payload,
        consent_training=consent_training.lower() in ("true", "1", "on", "yes"),
    )
    return JSONResponse(payload)


@app.post("/api/feedback")
async def feedback(rec_id: str = Form(...), actual: str = Form(""),
                   comment: str = Form("")):
    """Что реально нашли в сервисе. Замыкает цикл сбора данных."""
    corpus: Corpus = _state["corpus"]
    corpus.save_feedback(rec_id, actual=actual.strip(), comment=comment.strip())
    return {"ok": True}


@app.get("/api/stats")
async def stats():
    return _state["corpus"].stats()


@app.get("/api/health")
async def health():
    return {"ok": True, "engine": "engine" in _state}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
