"""FastAPI-сервис: запись звука из браузера, анализ, разбор механиком, корпус.

Запуск в разработке:
    uvicorn service.app:app --reload --port 8080

В проде смотри service/README.md: нужен HTTPS (без него браузер не даст доступ
к микрофону), выкачанный заранее CLAP и обратный прокси.

Модели грузятся один раз при старте: загрузка CLAP занимает около 9 секунд,
сам анализ — 0.23 секунды на CPU.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from service import mechanic
from service.diagnose import Engine
from service.storage import Corpus

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("service")

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_SECONDS = int(os.getenv("MAX_CLIP_SECONDS", "60"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))
CORPUS_DIR = os.getenv("CORPUS_DIR", "corpus")
ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o]

_state: dict = {}
_hits: dict[str, deque] = defaultdict(deque)

# Готовая акустика ждёт здесь, пока фронт запросит разбор механиком. На одном
# инстансе словаря достаточно; при нескольких репликах это переезжает в Redis.
PENDING_MAX = int(os.getenv("PENDING_MAX", "500"))
MAX_ROUNDS = int(os.getenv("MAX_REFINE_ROUNDS", "5"))
_pending: dict[str, dict] = {}


def _warmup(engine: Engine) -> None:
    """Прогнать эталонный клип, чтобы CLAP оказался в памяти до первого запроса.

    Classifier.load() поднимает только линейные головы; сам CLAP грузится лениво
    при первом diagnose и стоит около 9 секунд. Без прогрева эти 9 секунд платит
    первый пользователь после каждого деплоя: замер показал 10.9 с против 0.23 с
    на последующих запросах.
    """
    fixture = Path(__file__).resolve().parents[1] / "src" / "cardiag" / "_fixtures" / "demo.wav"
    if not fixture.is_file():
        log.warning("нет эталонного клипа для прогрева: %s", fixture)
        return
    t0 = time.perf_counter()
    try:
        engine.analyse(str(fixture))
        log.info("CLAP прогрет за %.1f c", time.perf_counter() - t0)
    except Exception as e:  # прогрев не критичен: сервис поднимется и без него
        log.warning("прогрев не удался (%s), первый запрос будет медленным", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.perf_counter()
    _state["engine"] = Engine()
    _state["corpus"] = Corpus(CORPUS_DIR)
    log.info("модели загружены за %.1f c", time.perf_counter() - t0)
    _warmup(_state["engine"])
    if not mechanic.api_key():
        log.warning("AIMLAPI_KEY не задан — разбор механиком будет отключён")
    yield
    _state.clear()


app = FastAPI(title="Диагностика по звуку", lifespan=lifespan,
              docs_url=os.getenv("DOCS_URL") or None, redoc_url=None)

if ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                       allow_methods=["POST", "GET"], allow_headers=["*"])


def _client_ip(request: Request) -> str:
    """За обратным прокси реальный адрес приходит в X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown")


def _rate_limit(request: Request) -> None:
    """Простое окно в час на адрес. На одном инстансе этого достаточно;
    при нескольких воркерах счётчик переезжает в Redis."""
    if RATE_LIMIT <= 0:
        return
    ip = _client_ip(request)
    now = time.time()
    hits = _hits[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(429, "Слишком много запросов. Попробуйте через час.")
    hits.append(now)


def _to_wav(src: Path, dst: Path) -> None:
    """Привести что угодно из браузера к моно 48 кГц — родному формату CLAP.

    MediaRecorder отдаёт webm/opus или mp4, а не WAV, поэтому ffmpeg обязателен.
    Длина режется: за пределами минуты полезного сигнала не прибавляется, а
    время анализа и место на диске растут.
    """
    if not shutil.which("ffmpeg"):
        raise HTTPException(500, "На сервере нет ffmpeg — конвертация невозможна.")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-t", str(MAX_SECONDS), "-ar", "48000", "-ac", "1", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        log.info("ffmpeg отказал: %s", proc.stderr[:200])
        raise HTTPException(400, "Не удалось прочитать аудио. Запишите ещё раз.")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/analyse")
async def analyse(
    request: Request,
    audio: UploadFile,
    brand: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    mileage: str = Form(""),
    symptom: str = Form(""),
    consent_training: str = Form("false"),
):
    _rate_limit(request)

    raw = await audio.read()
    if not raw:
        raise HTTPException(400, "Пустой файл.")
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {MAX_UPLOAD_MB} МБ.")

    corpus: Corpus = _state["corpus"]
    engine: Engine = _state["engine"]
    rec_id = corpus.new_id()
    vehicle = {"brand": brand.strip()[:40], "model": model.strip()[:40],
               "year": year.strip()[:4], "mileage": mileage.strip()[:8]}
    symptom = symptom.strip()[:500]

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "upload.bin"
        src.write_bytes(raw)
        wav = corpus.audio_path(rec_id)
        _to_wav(src, wav)
        try:
            report = engine.analyse(str(wav))
        except ValueError as e:
            raise HTTPException(400, f"Не удалось разобрать запись: {e}") from None
    acoustic_ms = int((time.perf_counter() - t0) * 1000)

    payload = report.to_dict()
    payload["id"] = rec_id
    payload["timing"] = {"acoustic_ms": acoustic_ms}
    # Разбор механиком доступен, только если есть на чём рассуждать.
    payload["mechanic_pending"] = bool(mechanic.api_key()) and report.status != "uncertain"

    corpus.save_record(
        rec_id, vehicle=vehicle, symptom=symptom, report=payload,
        consent_training=consent_training.lower() in ("true", "1", "on", "yes"),
    )
    # Разбор идёт вторым запросом: акустика готова за пару секунд, а LLM думает
    # около двенадцати. Держать готовый вердикт ради неё — терять пользователя.
    _pending[rec_id] = {"report": payload, "vehicle": vehicle,
                        "symptom": symptom, "messages": [], "rounds": 0}
    while len(_pending) > PENDING_MAX:
        _pending.pop(next(iter(_pending)))

    log.info("анализ %s: %s / %s за %d мс", rec_id, payload["status"],
             payload.get("zone", "-"), acoustic_ms)
    return JSONResponse(payload)


@app.post("/api/mechanic")
async def mechanic_opinion(rec_id: str = Form(...)):
    """Вторая фаза: разбор механиком по уже посчитанной акустике."""
    key = rec_id.strip()[:32]
    entry = _pending.get(key)
    if entry is None:
        raise HTTPException(404, "Анализ не найден или устарел. Запишите заново.")

    t0 = time.perf_counter()
    messages = mechanic.build_messages(entry["report"], entry["vehicle"],
                                       entry["symptom"])
    opinion, raw = mechanic._call(messages)
    took = int((time.perf_counter() - t0) * 1000)

    if opinion.ok and raw:
        # История нужна, чтобы уточнения продолжали тот же разговор, а не
        # начинали новый: механик должен помнить, что уже предполагал.
        entry["messages"] = messages + [{"role": "assistant", "content": raw}]

    body = opinion.to_dict()
    body["took_ms"] = took
    log.info("разбор %s: ok=%s за %d мс", key, opinion.ok, took)
    return JSONResponse(body)


@app.post("/api/mechanic/refine")
async def mechanic_refine(rec_id: str = Form(...), answers: str = Form(...)):
    """Уточнение: владелец отвечает на вопросы, механик пересматривает вывод."""
    key = rec_id.strip()[:32]
    entry = _pending.get(key)
    if entry is None:
        raise HTTPException(404, "Анализ не найден или устарел. Запишите заново.")
    if not entry.get("messages"):
        raise HTTPException(409, "Сначала нужен первичный разбор.")

    text = answers.strip()[:2000]
    if not text:
        raise HTTPException(400, "Напишите, что удалось выяснить.")
    if entry.get("rounds", 0) >= MAX_ROUNDS:
        raise HTTPException(429, "Достигнут предел уточнений для этой записи.")

    t0 = time.perf_counter()
    opinion, convo = mechanic.refine(entry["messages"], text)
    took = int((time.perf_counter() - t0) * 1000)

    if opinion.ok:
        entry["messages"] = convo
        entry["rounds"] = entry.get("rounds", 0) + 1
        _state["corpus"].save_clarification(key, answers=text,
                                            diagnosis=opinion.diagnosis,
                                            ruled_out=opinion.ruled_out)

    body = opinion.to_dict()
    body["took_ms"] = took
    body["rounds_left"] = MAX_ROUNDS - entry.get("rounds", 0)
    log.info("уточнение %s: ok=%s, раунд %d, за %d мс",
             key, opinion.ok, entry.get("rounds", 0), took)
    return JSONResponse(body)


@app.post("/api/feedback")
async def feedback(rec_id: str = Form(...), actual: str = Form(""),
                   comment: str = Form("")):
    """Что реально нашли в сервисе. Замыкает цикл сбора данных."""
    _state["corpus"].save_feedback(
        rec_id.strip()[:32], actual=actual.strip()[:200], comment=comment.strip()[:1000])
    return {"ok": True}


@app.get("/api/stats")
async def stats():
    return _state["corpus"].stats()


@app.get("/api/health")
async def health():
    return {
        "ok": "engine" in _state,
        "mechanic": bool(mechanic.api_key()),
        "model": mechanic.MODEL,
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }


app.mount("/static", StaticFiles(directory=STATIC), name="static")
