# Деплой «Что стучит»

От пустого сервера до работающего сайта — примерно 40 минут, из которых
30 занимает сборка образа.

## Что понадобится

- **Сервер:** 2 vCPU, 4 ГБ RAM, 40 ГБ диска. Модель занимает около 700 МБ в
  памяти, образ с CLAP — около 8 ГБ на диске. Из российских: Selectel,
  Timeweb, VK Cloud, Yandex Cloud. Годится любой с Ubuntu 22.04+.
- **Домен:** `chtostuchit.ru`. A-запись на IP сервера.
- **Ключ AIMLAPI** для разбора механиком.

GPU не нужен: анализ занимает 0.23 секунды на процессоре.

## 1. Домены

Их два: `chtostuchit.ru` и `чтостучит.рф`.

**Канонический — `chtostuchit.ru`.** Причина не во вкусе: кириллический адрес
в конфигах, сертификатах и заголовках существует только как punycode
`xn--h1aljccbf1af.xn--p1ai`, и при копировании в мессенджеры, почту и старый
софт он нередко ломается. Латиница работает везде. Кириллический домен остаётся
живым и отдаёт постоянный редирект на основной — так он приводит людей, но не
плодит для поиска две копии одного сайта.

В панели **каждого** регистратора добавьте по две A-записи на IP сервера:

```
@    A    <IP сервера>    300
www  A    <IP сервера>    300
```

Кириллический домен вводится в панели как есть, `чтостучит.рф`: в punycode его
переводит сам регистратор.

Проверьте, что записи разошлись. Без этого Caddy не выпустит сертификаты и
будет циклически пытаться заново:

```bash
dig +short chtostuchit.ru www.chtostuchit.ru xn--h1aljccbf1af.xn--p1ai www.xn--h1aljccbf1af.xn--p1ai
```

Все четыре строки должны показать один и тот же IP. Если пусто — DNS ещё не
обновился, подождите и повторите; выкатывать раньше смысла нет.

## 2. Сервер

```bash
ssh root@<IP>
```

Установите Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

Откройте порты:

```bash
ufw allow 22,80,443/tcp && ufw --force enable
```

## 3. Код

```bash
git clone -b feat/ru-service https://github.com/artem111-oss/car-diagnosis.git /var/www/chtostuchit
```

Если каталог уже занят прежней версией, обновите её на месте:

```bash
cd /var/www/chtostuchit && git fetch origin && git checkout feat/ru-service && git pull
```

## 4. Настройки

```bash
cp /var/www/chtostuchit/service/.env.example /var/www/chtostuchit/.env
```

Откройте `/var/www/chtostuchit/.env` и заполните:

```
AIMLAPI_KEY=<ваш ключ>
AIMLAPI_MODEL=anthropic/claude-haiku-latest
RATE_LIMIT_PER_HOUR=20
CORPUS_DIR=/data/corpus
```

Домены здесь не указываются — они прописаны в `Caddyfile`, потому что их два и
один редиректит на другой.

Закройте файл от посторонних: в нём платёжный ключ.

```bash
chmod 600 /var/www/chtostuchit/.env
```

## 5. Кто держит порты

Сначала посмотрите, свободны ли 80 и 443:

```bash
sudo ss -tlnp | grep -E ':(80|443)\s'
```

Пусто — сервер чистый, идите по **сценарию Б**. Видно `nginx` или `apache` —
**сценарий А**. Это частый случай, если код лежит в `/var/www`: путь как раз их
конвенция.

Публичный порт в обоих случаях остаётся 443. Менять его нельзя: адрес вида
`https://chtostuchit.ru:8443` никто не набирает, а ссылки на него ломаются в
мессенджерах. Настраиваемые порты — только внутренние.

## 5А. Запуск за существующим nginx

Приложение слушает только localhost — наружу с ним говорит nginx.

**Сначала выберите свободный порт.** На сервере могут работать чужие сервисы:

```bash
sudo ss -tlnp | grep -E ':(8080|8090|8091)\s'
```

По умолчанию берётся **8090**. Если он занят, поставьте другой и поменяйте его
в двух местах сразу — в `deploy/chtostuchit.service` и в `proxy_pass` внутри
`deploy/nginx.conf`. Чужие процессы не трогайте: они к сервису отношения не
имеют.

Поднимаем контейнер. Caddy при этом **не** запускается: он спрятан за профиль,
потому что 80 и 443 уже заняты nginx.

```bash
cd /var/www/chtostuchit && docker compose up -d --build
```

Проверяем, что порт слушается на localhost, а не наружу:

```bash
sudo ss -tlnp | grep 8090
```

Должен быть `127.0.0.1:8090`. Если адрес внешний — подтянулась старая версия
`docker-compose.yml`, сделайте `git pull`.

<details>
<summary>Альтернатива без Docker — systemd</summary>

```bash
sudo cp deploy/chtostuchit.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

```bash
sudo chown -R www-data:www-data /var/www/chtostuchit
```

```bash
sudo systemctl enable --now chtostuchit && sudo systemctl status chtostuchit
```

</details>

Ставим конфиг сайта:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/chtostuchit
```

```bash
sudo ln -sf /etc/nginx/sites-available/chtostuchit /etc/nginx/sites-enabled/
```

Выпускаем сертификаты на все четыре имени. Кириллический домен указывается
только в punycode — certbot кириллицу не понимает:

```bash
sudo certbot --nginx -d chtostuchit.ru -d www.chtostuchit.ru -d xn--h1aljccbf1af.xn--p1ai -d www.xn--h1aljccbf1af.xn--p1ai
```

Проверяем и перезагружаем:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 5Б. Запуск на чистом сервере

Caddy сам получит и продлит TLS:

```bash
cd /var/www/chtostuchit && docker compose --profile caddy up -d --build
```

## Сборка

Первая сборка тянет torch и запекает CLAP в образ — это те самые 30 минут.
Зато контейнер стартует мгновенно и не ходит в HuggingFace при каждом деплое.

Следите за логом:

```bash
docker compose logs -f app
```

Готовность — строка `CLAP прогрет за 8.8 c`, следом `Application startup complete`.

## 6. Проверка

Сервис жив:

```bash
curl -s https://chtostuchit.ru/api/health
```

Ожидаемый ответ:

```json
{"ok":true,"mechanic":true,"model":"anthropic/claude-haiku-latest","ffmpeg":true}
```

`mechanic:false` означает, что ключ не подхватился: проверьте `.env` и
перезапустите `docker compose up -d`.

Оба домена и все редиректы:

```bash
for h in chtostuchit.ru www.chtostuchit.ru xn--h1aljccbf1af.xn--p1ai www.xn--h1aljccbf1af.xn--p1ai; do echo -n "$h -> "; curl -sI "https://$h" | head -1; done
```

Канонический должен отвечать `200`, остальные три — `301`. Сертификаты Caddy
выпускает на все четыре имени сам, но не мгновенно: первые запросы после
запуска могут упасть, пока идёт выпуск. Смотрите `docker compose logs caddy`.

Дальше откройте сайт с телефона и запишите звук. **Проверять надо именно с
телефона по HTTPS**: браузер не даёт доступ к микрофону на незащищённом
соединении, и это единственный способ убедиться, что главная функция работает.

## Если сервис уже запущен без Docker

Когда uvicorn работает на хосте напрямую (systemd или запуск руками), Caddy
ставится отдельно, а в `Caddyfile` меняется одна строка:

```
reverse_proxy 127.0.0.1:8080
```

вместо `reverse_proxy app:8080`. Остальной конфиг — домены, редиректы,
заголовки, лимит тела запроса — работает как есть.

```bash
cp /var/www/chtostuchit/Caddyfile /etc/caddy/Caddyfile && systemctl reload caddy
```

## Обновление

```bash
cd /var/www/chtostuchit && git pull && docker compose up -d --build
```

Корпус лежит в docker volume и пересборку переживает.

## Резервные копии

Собранные записи — единственное, что нельзя восстановить. Всё остальное
воспроизводится из git.

```bash
docker run --rm -v chtostuchit_corpus:/data -v /root:/backup alpine tar czf /backup/corpus-$(date +%F).tar.gz -C /data .
```

Поставьте это в cron раз в сутки и увозите архив с сервера.

## Что смотреть в работе

```bash
docker compose logs -f app | grep -E "анализ|разбор|уточнение"
```

Три метрики, которых нет в обычном мониторинге:

- **Доля `uncertain`.** Растёт — значит либо люди записывают плохо (чинится
  текстом в мастере записи), либо пошли машины, которых модель не знает
  (чинится дообучением).
- **Доля `conflict`.** Головы модели разошлись; при росте пора переобучать.
- **Совпадение с реальностью.** `GET /api/stats`, поле `matched_top_part`
  против `with_feedback`. Единственная честная метрика качества.

## Когда упрётесь в потолок

Один воркер держит модель в памяти. Масштабироваться нужно репликами, а не
воркерами внутри контейнера, иначе память умножится на их число.

Перед этим шагом вынесите из памяти процесса два места: `_pending` (готовая
акустика в ожидании разбора) и счётчик лимита запросов. Оба переезжают в Redis
без переделки логики. `corpus/` к этому моменту стоит перевести на S3, а
`records.jsonl` — в PostgreSQL.

## Стоимость

| Статья | В месяц |
|---|---|
| Сервер 2 vCPU / 4 ГБ | 1 500–3 000 ₽ |
| Домен `.ru` | около 30 ₽ |
| AIMLAPI, разбор механиком | около 1,1 ₽ за анализ |

При 3 000 анализов в месяц выходит примерно 5–6 тысяч рублей. Разбор механиком
имеет смысл сделать платной функцией: акустика почти бесплатна, а LLM —
единственная статья, растущая линейно с числом пользователей.

## Если что-то не работает

**Сертификат не выдался.** Caddy не смог подтвердить домен: проверьте
`dig +short chtostuchit.ru` и что порт 80 открыт. Для проверки Let's Encrypt
ходит именно на 80, даже когда сайт работает по 443.

**Кириллический домен не открывается, латинский работает.** Почти всегда DNS:
`dig +short xn--h1aljccbf1af.xn--p1ai` должен вернуть тот же IP. Проверять надо
punycode-имя, а не `чтостучит.рф` — часть утилит кириллицу не переводит.

**Обрыв на `docker compose up` с жалобой на DOMAIN.** Осталась старая версия
`docker-compose.yml`, где домен брался из `.env`. Обновите репозиторий: теперь
домены живут в `Caddyfile`.

**`failed to bind host port 0.0.0.0:80: address already in use`.** Порт занят
другим веб-сервером — это сценарий А, а не Б. Остановите Caddy и работайте
через существующий nginx:

```bash
docker compose --profile caddy down && docker compose up -d
```

**502 от nginx.** Приложение не отвечает на том порту, куда стучится
`proxy_pass`. Сверьте порт в `deploy/nginx.conf` с тем, на котором слушает
сервис, и проверьте, что он жив: `sudo systemctl status chtostuchit` либо
`docker compose ps`.

**Сервис слушает публичный IP.** Проверьте `sudo ss -tlnp | grep uvicorn`:
адрес должен быть `127.0.0.1`, а не внешний. Если внешний — запущен старый
процесс мимо systemd, снимите его через `sudo pkill -f 'uvicorn service.app'`
и поднимите юнитом.

**`Permission denied` при записи корпуса.** Юнит работает от `www-data`, а
каталог принадлежит `root`: `sudo chown -R www-data:www-data /var/www/chtostuchit`.

**504 на разборе механика, вердикт при этом приходит.** В nginx остались
дефолтные 60 секунд. Разбор доходит до минуты, поэтому в конфиге стоит
`proxy_read_timeout 180s` — проверьте, что он на месте.

**Микрофон не спрашивают.** Сайт открыт по HTTP. Браузер молча не даёт доступ
без TLS — сообщение об этом сервис показывает сам.

**`mechanic:false`** в health. Ключ не прочитан. Частая причина — файл `.env`
сохранён с BOM: пересоздайте его обычным текстовым редактором.

**Контейнер перезапускается.** `docker compose logs app`. Чаще всего не хватило
памяти при загрузке модели — нужно 4 ГБ.
