"""FastAPI-сервис: запись звука из браузера, анализ, разбор механиком, корпус.

Запуск в разработке:
    uvicorn service.app:app --reload --port 8080

В проде смотри service/README.md: нужен HTTPS (без него браузер не даст доступ
к микрофону), выкачанный заранее CLAP и обратный прокси.

Модели грузятся один раз при старте: загрузка CLAP занимает около 9 секунд,
сам анализ — 0.23 секунды на CPU.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from service import indexnow, label_mapper, mechanic, seo_pages, vehicle_facts
from service.diagnose import Engine
from service.memory_store import MemoryStore
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
    # Тот же смонтированный том, что уже переживает пересборку образа —
    # никакой новой инфраструктуры, обычный файл SQLite.
    _state["memory"] = MemoryStore(Path(CORPUS_DIR) / "memory.db")
    log.info("модели загружены за %.1f c", time.perf_counter() - t0)
    _warmup(_state["engine"])
    if not mechanic.api_key():
        log.warning("AIMLAPI_KEY не задан — разбор механиком будет отключён")
    # IndexNow: сообщить о всех страницах при каждом старте. Идемпотентно и
    # дёшево — если ничего не изменилось, поисковик просто ничего не найдёт
    # нового; зато новая страница не ждёт планового обхода.
    if not os.getenv("DISABLE_INDEXNOW"):
        asyncio.get_event_loop().run_in_executor(
            None, indexnow.submit, "chtostuchit.ru", seo_pages.all_paths())
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


@app.get("/robots.txt")
async def robots():
    return PlainTextResponse(
        "User-agent: *\nAllow: /\n\nSitemap: https://chtostuchit.ru/sitemap.xml\n")


@app.get("/sitemap.xml")
async def sitemap():
    urls = "".join(f"<url><loc>https://chtostuchit.ru{p}</loc></url>" for p in seo_pages.all_paths())
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemap.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml")


@app.get(f"/{indexnow.KEY}.txt")
async def indexnow_key():
    """Файл-подтверждение владения доменом для IndexNow. Ключ фиксированный —
    см. service/indexnow.py."""
    return PlainTextResponse(indexnow.KEY)


@app.get("/statistika")
async def statistika():
    """Публичная страница честности: то, что /api/stats считает для оператора,
    здесь видит любой посетитель — до подписки, до просьбы довериться."""
    s = _state["corpus"].stats()
    m = _state["memory"].stats()
    return HTMLResponse(seo_pages.stats_page(s, m))


@app.get("/partnyorstvo")
async def partner_landing():
    return HTMLResponse(seo_pages.partner_page())


@app.post("/api/partners")
async def submit_partner(name: str = Form(...), city: str = Form(...),
                         contact: str = Form(...), note: str = Form("")):
    """Заявка от СТО. Не появляется публично, пока не одобрена — бейдж
    выдаётся после реального контакта, а не автоматически по факту формы."""
    if not name.strip() or not city.strip() or not contact.strip():
        raise HTTPException(400, "Заполните название, город и контакт.")
    _state["memory"].add_partner(
        name=name.strip()[:120], city=city.strip()[:60],
        contact=contact.strip()[:120], note=note.strip()[:500])
    return {"ok": True}


@app.get("/api/partners")
async def list_partners():
    """Одобренные партнёры — для бейджей на главной. Без approved=1 внутри
    memory_store список пуст, что и есть корректное поведение до первого
    реального рукопожатия."""
    return {"partners": _state["memory"].list_partners(approved_only=True)}


@app.post("/api/analyse")
async def analyse(
    request: Request,
    audio: UploadFile,
    brand: str = Form(""),
    model: str = Form(""),
    year: str = Form(""),
    mileage: str = Form(""),
    engine_spec: str = Form(""),
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
    # engine_spec — то, что владелец знает о моторе ("1.6, 8 клапанов"), не
    # путать с переменной engine выше (акустическая модель). На одну модель
    # часто ставят разные моторы с разным приводом ГРМ — без этого уточнения
    # справочник вынужден гадать между вариантами вместо точного ответа.
    vehicle = {"brand": brand.strip()[:40], "model": model.strip()[:40],
               "year": year.strip()[:4], "mileage": mileage.strip()[:8],
               "engine_spec": engine_spec.strip()[:80]}
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

    consent = consent_training.lower() in ("true", "1", "on", "yes")
    corpus.save_record(rec_id, vehicle=vehicle, symptom=symptom, report=payload,
                       consent_training=consent)
    # Разбор идёт вторым запросом: акустика готова за пару секунд, а LLM думает
    # около двенадцати. Держать готовый вердикт ради неё — терять пользователя.
    # consent живёт здесь же: он понадобится позже, если владелец подтвердит
    # диагноз через /api/feedback и запись пойдёт в обучающий корпус.
    _pending[rec_id] = {"report": payload, "vehicle": vehicle, "symptom": symptom,
                        "consent_training": consent, "messages": [], "rounds": 0}
    while len(_pending) > PENDING_MAX:
        _pending.pop(next(iter(_pending)))

    log.info("анализ %s: %s / %s за %d мс", rec_id, payload["status"],
             payload.get("zone", "-"), acoustic_ms)
    return JSONResponse(payload)


def _run_first_pass(entry: dict, memory: MemoryStore):
    """Справка по машине + первый разбор — синхронный блок, целиком идущий в
    отдельный поток (см. вызов ниже). Историю диалога кладём в entry здесь же:
    после to_thread у вызывающей стороны нет доступа к локальному messages."""
    facts = vehicle_facts.lookup_persistent(entry["vehicle"], memory)
    entry["facts"] = facts
    messages = mechanic.build_messages(entry["report"], entry["vehicle"],
                                       entry["symptom"], facts)
    opinion, raw = mechanic._call(messages)
    if opinion.ok and raw:
        # История нужна, чтобы уточнения продолжали тот же разговор, а не
        # начинали новый: механик должен помнить, что уже предполагал.
        entry["messages"] = messages + [{"role": "assistant", "content": raw}]
    return opinion


@app.post("/api/mechanic")
async def mechanic_opinion(rec_id: str = Form(...)):
    """Вторая фаза: разбор механиком по уже посчитанной акустике."""
    key = rec_id.strip()[:32]
    entry = _pending.get(key)
    if entry is None:
        raise HTTPException(404, "Анализ не найден или устарел. Запишите заново.")

    t0 = time.perf_counter()
    # Справка (Sonar Pro) и сам разбор (AIMLAPI) — блокирующие HTTP-вызовы на
    # десятки секунд каждый. Раньше они выполнялись прямо в async-хендлере и
    # держали event loop: любой другой запрос, включая /api/health, ждал бы
    # следом. asyncio.to_thread выносит всю синхронную цепочку в поток.
    opinion = await asyncio.to_thread(_run_first_pass, entry, _state["memory"])
    took = int((time.perf_counter() - t0) * 1000)

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
    opinion, convo = await asyncio.to_thread(mechanic.refine, entry["messages"], text)
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


@app.post("/api/prefetch")
async def prefetch(background: BackgroundTasks, brand: str = Form(""),
                   model: str = Form(""), year: str = Form(""),
                   engine_spec: str = Form("")):
    """Прогреть справочник, пока пользователь читает инструкцию и пишет звук.

    Справка занимает около 15 секунд и нужна только к моменту разбора. Владелец
    к этому времени успевает пройти два экрана и записать 15 секунд аудио, так
    что запрос укладывается в это окно и перестаёт стоить времени. Результат
    ложится в постоянный кэш (переживает редеплой), откуда его возьмёт разбор.
    """
    if not brand.strip():
        return {"ok": False}
    vehicle = {"brand": brand.strip()[:40], "model": model.strip()[:40],
              "year": year.strip()[:4], "engine_spec": engine_spec.strip()[:80]}
    background.add_task(vehicle_facts.lookup_persistent, vehicle, _state["memory"])
    return {"ok": True}


def _classify_correction(memory: MemoryStore, correction_id: int, owner_text: str,
                         symptom: str, ai_status: str, ai_top_part: str) -> None:
    """Разметить свободный текст владельца одним из 21 канонического класса —
    фоном, после того как ответ '{"ok": true}' уже ушёл пользователю."""
    label, confidence = label_mapper.classify(
        owner_text, symptom=symptom, ai_status=ai_status, ai_top_part=ai_top_part)
    memory.set_mapped_label(correction_id, label, confidence)
    log.info("разметка исправления #%s: %s (%s)", correction_id, label or "—", confidence)


@app.post("/api/feedback")
async def feedback(background: BackgroundTasks, rec_id: str = Form(...),
                   actual: str = Form(""), comment: str = Form("")):
    """Что реально нашли в сервисе. Замыкает цикл сбора данных двояко: как и
    раньше пишет в corpus/feedback.jsonl, и — если владелец назвал причину и
    исходно согласился на обучение — заводит строку в таблице corrections,
    которую следующим шагом разметит фоновый классификатор и подготовит к
    `cardiag ingest --cause <класс>` (service/export_training_set.py)."""
    key = rec_id.strip()[:32]
    actual = actual.strip()[:200]
    comment = comment.strip()[:1000]

    _state["corpus"].save_feedback(key, actual=actual, comment=comment)

    entry = _pending.get(key)
    if actual and entry is not None and entry.get("consent_training"):
        report = entry["report"]
        ai_top_part = (report.get("versions") or [{}])[0].get("part", "")
        correction_id = _state["memory"].save_correction(
            rec_id=key, vehicle_key=vehicle_facts.vehicle_key(entry["vehicle"]),
            vehicle=entry["vehicle"], symptom=entry["symptom"],
            ai_status=report.get("status", ""), ai_zone=report.get("zone", ""),
            ai_top_part=ai_top_part, owner_text=actual, comment=comment,
            audio_path=str(_state["corpus"].audio_path(key)),
            consent_training=True,
        )
        background.add_task(_classify_correction, _state["memory"], correction_id,
                            actual, entry["symptom"], report.get("status", ""),
                            ai_top_part)
    elif actual and entry is None:
        log.info("фидбек %s без контекста в _pending — в обучающий корпус не пойдёт", key)

    return {"ok": True}


@app.get("/api/stats")
async def stats():
    s = _state["corpus"].stats()
    s["memory"] = _state["memory"].stats()
    return s


@app.get("/api/health")
async def health():
    return {
        "ok": "engine" in _state,
        "mechanic": bool(mechanic.api_key()),
        "model": mechanic.MODEL,
        "ffmpeg": bool(shutil.which("ffmpeg")),
    }


@app.get("/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def seo_page(slug: str):
    """Симптомные SEO-страницы. Регистрируется последним: однословный путь
    иначе перехватил бы будущие /api/* или другие короткие маршруты."""
    page = seo_pages.PAGES.get(f"/{slug}")
    if page is None:
        raise HTTPException(404)
    return HTMLResponse(page)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
