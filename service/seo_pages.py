"""Серверный HTML для SEO: симптомные страницы + публичная страница честности.

Не SPA — эти страницы должны индексироваться Яндексом без выполнения JS,
поэтому весь контент лежит прямо в HTML, а не рендерится скриптом мастера.
"""
from __future__ import annotations

import json

BRAND = "Что стучит"
DOMAIN = "https://chtostuchit.ru"

_STYLE = """
:root{color-scheme:dark;--bg:#0e1211;--card:#171e1b;--card2:#1d2622;--line:#26302b;
  --ink:#eaefea;--ink2:#a7b2aa;--ink3:#717d75;--amber:#f0872e;--amber2:#ffb46b;
  --green:#3fb87a;--yellow:#dfa93a;--red:#e2635c;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,Consolas,monospace;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.6;font-size:16.5px}
.wrap{max-width:640px;margin:0 auto;padding:0 20px 60px}
header{padding:20px 0;border-bottom:1px solid var(--line);margin-bottom:26px;display:flex;align-items:center;gap:10px}
header a{color:var(--ink);text-decoration:none;font-weight:680;font-size:15.5px}
h1{font-size:clamp(25px,6vw,33px);line-height:1.15;margin:0 0 14px;letter-spacing:-.02em}
h2{font-size:19px;margin:26px 0 10px}
p{color:var(--ink2)}
p.lede{font-size:18px;color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0}
.cause{border:1px solid var(--line);border-radius:10px;padding:13px 15px;margin-bottom:9px;background:var(--card2)}
.cause b{color:var(--ink)}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:5px;font-weight:600;margin-left:6px}
.badge.high{background:rgba(226,99,92,.2);color:#f0a9a5}
.badge.med{background:rgba(223,169,58,.2);color:#e8cb8a}
.badge.low{background:rgba(63,184,122,.2);color:#8fd6ab}
.cta{display:block;text-align:center;background:var(--amber);color:#1c0d02;text-decoration:none;
  font-weight:680;padding:15px;border-radius:11px;margin:22px 0;font-size:16px}
.faq b{color:var(--ink);display:block;margin-bottom:4px}
.faq div{margin-bottom:16px}
footer{color:var(--ink3);font-size:13px;text-align:center;padding-top:20px;border-top:1px solid var(--line);margin-top:30px}
"""


def _shell(*, path: str, title: str, description: str, h1: str, lede: str,
          body: str, faq: list[tuple[str, str]]) -> str:
    faq_ld = {
        "@context": "https://schema.org", "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in faq
        ],
    }
    faq_html = "".join(f'<div class="faq"><b>{q}</b><span>{a}</span></div>' for q, a in faq)
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="{DOMAIN}{path}">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<meta name="description" content="{description}">
<title>{title}</title>
<script type="application/ld+json">{json.dumps(faq_ld, ensure_ascii=False)}</script>
<style>{_STYLE}</style>
</head><body>
<header><div class="wrap" style="border:none;padding:0;margin:0;display:flex;align-items:center;gap:10px">
<a href="/">{BRAND}</a></div></header>
<div class="wrap">
<h1>{h1}</h1>
<p class="lede">{lede}</p>
{body}
<h2>Частые вопросы</h2>
{faq_html}
<a class="cta" href="/">Записать звук и проверить бесплатно →</a>
<footer>{BRAND} — предварительная подсказка по звуку, не заменяет осмотр специалиста.</footer>
</div></body></html>"""


def _causes_page(*, path, title, description, h1, lede, causes, faq):
    body = "".join(
        f'<div class="cause"><b>{name}</b><span class="badge {level}">{level_ru}</span>'
        f'<p style="margin:6px 0 0">{desc}</p></div>'
        for name, level, level_ru, desc in causes
    )
    return _shell(path=path, title=title, description=description, h1=h1, lede=lede,
                 body=body, faq=faq)


PAGES: dict[str, str] = {
    "/stuk-v-dvigatele-na-holodnuyu": _causes_page(
        path="/stuk-v-dvigatele-na-holodnuyu",
        title="Стук в двигателе на холодную: причины и как понять по звуку — Что стучит",
        description="Ровное цоканье или глухой стук в двигателе на холодную — разбор частых причин по симптому и бесплатная проверка по записи звука.",
        h1="Стук в двигателе на холодную",
        lede="Звук, который слышен первые минуты после запуска и пропадает после прогрева, почти всегда механический, а не про топливо или зажигание. Вот что это обычно значит.",
        causes=[
            ("Гидрокомпенсаторы / клапанный механизм", "med", "часто",
             "Ровное цоканье, похожее на швейную машинку. Проявляется на холодную, потому что масло ещё не прокачалось по системе. Обычно тише или пропадает после прогрева и на нагрузке."),
            ("Низкий уровень или неподходящее масло", "high", "проверить в первую очередь",
             "Самая дешёвая и частая причина — щуп покажет за минуту. Густое, старое или недолитое масло не создаёт нужного давления, пока двигатель холодный."),
            ("Цепь или ремень ГРМ, натяжитель", "med", "средняя вероятность",
             "Металлический дребезжащий стук, который может сохраняться и после прогрева, если натяжитель ослаб. На одной модели встречаются оба варианта привода — важно знать какой именно у вашего мотора."),
            ("Стук поршня / шатунного вкладыша", "high", "срочно к механику",
             "Глухой, «стучащий по кастрюле» звук, который редко пропадает полностью. Продолжать ездить с этим — риск дорогого ремонта."),
        ],
        faq=[
            ("Можно ли ездить, если стучит только на холодную?",
             "Если звук ровный, тихий и полностью пропадает после прогрева — обычно можно доехать до сервиса. Если стук глухой, громкий или сохраняется на прогретом двигателе — лучше не нагружать мотор и ехать на диагностику как можно скорее."),
            ("Как отличить компенсаторы от более серьёзной проблемы по звуку?",
             "На слух это сложно сделать надёжно — оба звука механические и могут быть похожи. Быстрее и дешевле записать звук и проверить бесплатно, чем гадать."),
        ],
    ),
    "/stuk-v-podveske-na-nerovnostyah": _causes_page(
        path="/stuk-v-podveske-na-nerovnostyah",
        title="Стук в подвеске на кочках и неровностях — причины по звуку — Что стучит",
        description="Стук спереди или сзади на неровной дороге — разбор частых причин (стойки, сайлентблоки, стабилизатор) и бесплатная проверка по записи звука.",
        h1="Стук в подвеске на неровностях",
        lede="Стук, который слышен только на кочках, лежачих полицейских и разбитом асфальте, почти всегда указывает на износ конкретного узла подвески — и часто его можно локализовать по характеру звука.",
        causes=[
            ("Стойки стабилизатора", "high", "самая частая причина",
             "Короткий сухой стук на каждой мелкой неровности, отчётливее на низкой скорости. Одна из самых дешёвых деталей подвески в замене."),
            ("Амортизаторы / стойки", "med", "проверить на пробое",
             "Глухой удар на крупных неровностях и «пробоях», может сопровождаться подтёками масла на самой стойке."),
            ("Сайлентблоки рычагов", "med", "стук + люфт",
             "Стук на кочках вместе с ощущением люфта в рулевом на разгоне/торможении. Часто требует диагностики на подъёмнике для точной локализации."),
            ("Опоры (подушки) двигателя или КПП", "low", "реже, но бывает",
             "Стук, который сильнее ощущается при трогании и переключении передач, а не только на кочках — стоит проверить отдельно."),
        ],
        faq=[
            ("Опасно ли ехать со стуком в подвеске?",
             "Стук сам по себе редко критичен для безопасности сразу, но изношенная подвеска ухудшает управляемость и тормозной путь. Затягивать с диагностикой не стоит."),
            ("Почему стук слышен только на кочках, а не всегда?",
             "Большинство узлов подвески (стойки стабилизатора, сайлентблоки, амортизаторы) стучат именно при сжатии-разжатии — то есть при проезде неровности, а не в статике или на ровной дороге."),
        ],
    ),
}


def stats_page(corpus: dict, memory: dict) -> str:
    """Публичная страница честности. То же самое, что видит оператор в
    /api/stats, но открыто любому посетителю — до подписки, до просьбы
    довериться на слово."""
    matched = corpus.get("matched_top_part", 0)
    with_fb = corpus.get("with_feedback", 0)
    accuracy_line = (
        f'<div class="card"><b style="color:var(--ink);font-size:22px">{matched} из {with_fb}</b>'
        f'<p style="margin:4px 0 0">подтверждённых случаев, где версия сервиса совпала с тем, '
        f'что реально нашли на СТО.</p></div>'
        if with_fb else
        '<div class="card"><p style="margin:0">Подтверждённых случаев пока мало для честной '
        'цифры точности — она появится здесь, как только наберётся статистика. '
        'Мы не публикуем оценки точности, которые нельзя проверить.</p></div>'
    )
    body = f"""
{accuracy_line}
<div class="card">
  <p style="margin:0 0 8px"><b style="color:var(--ink)">{corpus.get('records', 0)}</b> звуковых записей проанализировано</p>
  <p style="margin:0 0 8px"><b style="color:var(--ink)">{with_fb}</b> владельцев подтвердили или опровергли диагноз</p>
  <p style="margin:0 0 8px"><b style="color:var(--ink)">{memory.get('corrections_mapped', 0)}</b> случаев размечены и готовятся в обучающий корпус</p>
  <p style="margin:0"><b style="color:var(--ink)">{corpus.get('conflicts', 0)}</b> раз модель сама заметила противоречие между зоной и деталью и не стала называть деталь</p>
</div>
<p>Цифры считаются автоматически из <code>/api/stats</code>, без ручной коррекции. Конфликты голов модели не прячутся — их доля показывает, где сервис ещё не уверен в себе.</p>
"""
    return _shell(
        path="/statistika", title="Насколько точно работает сервис — Что стучит",
        description="Открытая статистика точности диагностики по звуку: сколько случаев подтверждено, сколько раз модель совпала с реальным диагнозом.",
        h1="Насколько это точно на самом деле",
        lede="Не маркетинговые «98%», а числа прямо из базы сервиса.",
        body=body, faq=[])
