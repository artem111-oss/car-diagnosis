# Образ прогревает CLAP на этапе сборки. Иначе первый пользователь после
# каждого деплоя ждёт, пока с HuggingFace приедут 2 ГБ — а из России этот
# канал ещё и нестабилен.
FROM python:3.11-slim AS base

# ffmpeg обязателен: браузер отдаёт webm/opus, модель ждёт WAV 48 кГц моно.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    HF_HOME=/models/hf \
    CORPUS_DIR=/data/corpus

WORKDIR /app

# Слой зависимостей отдельно от кода: правки в service/ не пересобирают torch.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web]" \
 && pip install --no-cache-dir httpx

COPY models ./models
COPY service ./service

# Прогрев: тянем CLAP в образ, чтобы старт контейнера был мгновенным.
RUN python -c "\
from transformers import ClapModel, ClapProcessor; \
ClapModel.from_pretrained('laion/clap-htsat-unfused'); \
ClapProcessor.from_pretrained('laion/clap-htsat-unfused'); \
print('CLAP прогрет')"

RUN useradd -m -u 10001 app \
 && mkdir -p /data/corpus \
 && chown -R app:app /data /models /app
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

# Один воркер держит модель в памяти (~700 МБ). Больше воркеров — кратно
# больше памяти, поэтому масштабироваться лучше репликами за прокси.
CMD ["uvicorn", "service.app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
