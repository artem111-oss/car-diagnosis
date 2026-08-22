"""Продуктовый слой поверх cardiag: согласование голов и честная подача.

Сырой вывод модели показывать владельцу машины нельзя по двум причинам.

Первая: головы region и cause обучались раздельно, поэтому спокойно выдают
несовместимую пару. На demo.wav из репозитория это зона «подвеска» и деталь
«выхлоп» одновременно.

Вторая: в models/best_model_clap.joblib температуры равны {'cause': 1.0,
'region': 1.0}, то есть эти две головы не откалиброваны вообще. Их 0.98 — сырой
softmax, а не вероятность. Откалиброваны только kind (3.081) и knock (1.726),
плюс отдельная модель триажа. Поэтому заголовок ответа строится на триаже, а
зона и деталь идут версиями без процентов.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from cardiag import Classifier
from cardiag.inference.triage import TriageClassifier

from service.labels_ru import URGENCY_RU, part_ru, region_ru, zones_agree

# Порог, ниже которого версия не показывается совсем: перечислять шум незачем.
_MIN_SHOW = 0.08

BAND_RU = {
    "high": "Высокая уверенность",
    "medium": "Средняя уверенность",
    "low": "Низкая уверенность",
    "abstain": "Не берусь судить",
}

# Русские пояснения к калиброванным полосам. Формулировки взяты из смысла
# reliability-таблицы в cardiag.inference.triage, а не придуманы.
BAND_GLOSS_RU = {
    "high": "Примерно в 9 случаях из 10 такой вывод оказывался верным.",
    "medium": "Примерно в 8 случаях из 10 такой вывод оказывался верным.",
    "low": "Это склонность, а не вывод. Нужна проверка.",
    "abstain": "Звука недостаточно, чтобы сделать вывод.",
}

TRIAGE_RU = {
    "engine": "Похоже на звук изнутри двигателя",
    "chassis": "Похоже на ходовую часть",
}


@dataclass
class Version:
    """Одна версия: узел, зона, срочность, подсказка."""
    part: str
    name: str
    zone: str
    zone_name: str
    urgency: str
    urgency_name: str
    hint: str
    agrees_with_zone: bool
    p_raw: float


@dataclass
class Report:
    """То, что уходит в интерфейс. Без сырых процентов по cause и region."""
    status: str                  # fault | normal | uncertain
    headline: str
    band: str
    band_name: str
    band_gloss: str
    zone: str = ""
    zone_name: str = ""
    versions: list[Version] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    conflict: bool = False
    clean_seconds: float = 0.0
    total_seconds: float = 0.0
    disclaimer: str = (
        "Это предварительная подсказка по звуку, а не диагностика. "
        "Она не заменяет осмотр специалиста."
    )
    debug: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["versions"] = [asdict(v) if not isinstance(v, dict) else v
                         for v in self.versions]
        return d


class Engine:
    """Держит обе модели в памяти. Загружать на каждый запрос нельзя:
    загрузка CLAP занимает ~9 секунд, сам инференс — 0.23."""

    def __init__(self, model_dir: str | Path | None = None):
        self._clf = Classifier.load(str(model_dir) if model_dir else None)
        self._triage = TriageClassifier.load(
            str(Path(model_dir) / "triage_model.joblib") if model_dir else None)

    def analyse(self, wav_path: str) -> Report:
        d = self._clf.diagnose(wav_path, clean_audio=True)
        t = self._triage.triage(wav_path)

        clean_s = round(sum(s.duration for s in d.segments), 1)
        total_s = round(max((s.end for s in d.segments), default=0.0), 1)

        top_zone = d.regions[0] if d.regions else None
        zone_key = top_zone.zone if top_zone else ""

        versions: list[Version] = []
        for c in d.causes:
            if c.p < _MIN_SHOW:
                continue
            info = part_ru(c.part)
            versions.append(Version(
                part=c.part,
                name=info.name,
                zone=info.zone,
                zone_name=region_ru(info.zone),
                urgency=info.urgency,
                urgency_name=URGENCY_RU[info.urgency],
                hint=info.hint,
                agrees_with_zone=zones_agree(zone_key, c.part) if zone_key else False,
                p_raw=round(float(c.p), 3),
            ))

        conflict = bool(versions and zone_key and not versions[0].agrees_with_zone)

        # Фильтруем один раз здесь, а не в каждом клиенте. Наружу уходит только
        # то, что действительно можно показать: иначе интерфейс говорит «деталь
        # не называю» и тут же её описывает.
        if d.verdict.value != "fault":
            visible: list[Version] = []
        elif conflict:
            visible = [v for v in versions if v.agrees_with_zone]
        else:
            visible = versions

        band = t.band.value
        # Полоса приходит из триажа, а статус — из головы kind, и они могут
        # разойтись: «не могу определить» рядом со «средней уверенностью»
        # читается как сбой сервиса. На неуверенном вердикте полосу приводим
        # к тому же смыслу, что и заголовок.
        if d.verdict.value == "uncertain":
            band = "abstain"

        report = Report(
            status=d.verdict.value,
            headline=self._headline(d.verdict.value, t.triage, band),
            band=band,
            band_name=BAND_RU.get(band, band),
            band_gloss=BAND_GLOSS_RU.get(band, t.band_gloss),
            zone=zone_key,
            zone_name=region_ru(zone_key) if zone_key and d.verdict.value == "fault" else "",
            versions=visible,
            conflict=conflict,
            clean_seconds=clean_s,
            total_seconds=total_s,
            debug={
                "versions_suppressed": len(versions) - len(visible),
                "fault_probability": d.fault_probability,
                "knock_probability": d.engine_knock_probability,
                "triage": t.triage,
                "triage_confidence": round(t.confidence, 3),
                "regions": [r.to_dict() for r in d.regions],
                "causes": [c.to_dict() for c in d.causes],
                "segments": [s.to_dict() for s in d.segments],
            },
        )
        report.next_steps = self._next_steps(report, d.engine_knock_probability)
        return report

    @staticmethod
    def _headline(status: str, triage_label: str, band: str) -> str:
        if status == "uncertain" or band == "abstain":
            return "Не могу уверенно определить по этой записи"
        if status == "normal":
            return "Явной неисправности в звуке не слышу"
        return TRIAGE_RU.get(triage_label, "Слышу нехарактерный звук")

    @staticmethod
    def _next_steps(r: Report, knock_p: float) -> list[str]:
        """Всегда даём действие. Неуверенность модели не должна читаться как
        отказ сервиса — иначе продукт выглядит сломанным."""
        steps: list[str] = []

        if r.status == "uncertain" or r.band == "abstain":
            steps.append(
                "Перезапишите на прогретом двигателе, на холостых оборотах, "
                "без музыки и кондиционера.")
            steps.append(
                "Записывайте на стоянке, а не в движении: шум дороги перебивает дефект.")
            if r.clean_seconds < 5:
                steps.append(
                    f"В записи всего {r.clean_seconds} сек полезного звука — "
                    "нужно хотя бы 10 секунд непрерывно.")
            return steps

        if r.status == "normal":
            steps.append(
                "Если звук всё же слышен, запишите его в момент появления — "
                "на холодную, при повороте или при торможении.")
            return steps

        if r.versions:
            top = r.versions[0]
            steps.append(top.hint)
            if top.urgency in ("critical", "high"):
                steps.append("Не откладывайте визит в сервис.")

        if knock_p > 0.5:
            steps.insert(0, "Возможен стук в двигателе. Проверьте уровень масла "
                            "и не нагружайте мотор до осмотра.")

        if r.conflict:
            steps.append(
                "Версии по узлу расходятся с зоной, поэтому конкретной детали "
                "не называю: ориентируйтесь на зону.")
        return steps
