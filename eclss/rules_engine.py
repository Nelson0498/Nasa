"""
eclss.rules_engine — Motor de correlación riesgo-síntoma y de autonomía.

Este módulo implementa las tres capas del protocolo descrito en la
especificación funcional:

    1. **Reglas atómicas** — un KPI contra su umbral (ambiental, microbiológico
       o biomarcador). Responden «¿qué está fuera de rango?».
    2. **Rutas fisiopatológicas** (R1, R2, R3) — conjunciones de señales
       ambientales y biológicas que documentan el *mecanismo*. Responden «¿qué
       le está pasando a la tripulación y por qué?».
    3. **Escenarios de autonomía** (A, B, C) — conjunciones exactas del árbol de
       decisión sin Tierra. Responden «¿qué hacemos ahora, sin esperar 24 min?».

Sobre las conjunciones, dos decisiones de ingeniería que conviene explicitar:

* Un criterio temporal no puede evaluarse en una ventana de 24 h si su
  definición exige varios días («3 noches consecutivas de ruido», «ratio de
  cortisol aplanado 3 días»). Por eso esas reglas declaran su propio
  *lookback* (`EngineConfig.noise_lookback_hours`,
  `cortisol_lookback_days`) independiente de la ventana de análisis. Mezclar
  ambas cosas produciría un árbol de decisión que nunca dispara en órbita.
* La ausencia de datos **no** satisface un criterio: `UNKNOWN` nunca equivale a
  «normal». Si falta la muestra de saliva, la ruta no se cierra en verde, se
  queda en `UNKNOWN` y la UI debe mostrarlo como «datos insuficientes».

El motor es puro respecto de la base de datos: recibe un `Repository`, lee
ventanas, y devuelve objetos; la persistencia es un paso final explícito
(`persist=True`). Eso permite testearlo contra snapshots en memoria.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from . import config
from .config import (
    AlertStatus,
    Analyte,
    EngineConfig,
    Route,
    Scenario,
    SensorKind,
    Severity,
    THRESHOLD_VERSION,
    worst,
)
from .db import CrewRecord, Repository, to_iso, utcnow

# =========================================================================== #
# 1. Estructuras de datos
# =========================================================================== #

Series = list[tuple[datetime, float]]


@dataclass(slots=True)
class SeriesStats:
    """Resumen estadístico de una serie dentro de la ventana."""

    n: int = 0
    mean: float | None = None
    vmin: float | None = None
    vmax: float | None = None
    last: float | None = None
    p05: float | None = None
    p95: float | None = None

    @property
    def empty(self) -> bool:
        return self.n == 0


@dataclass(slots=True)
class Evidence:
    """Un dato concreto que sostiene una alerta (auditable y citable en la UI)."""

    kind: str                     # ENV | MICRO | BIOMARKER | SYMPTOM
    ref_table: str
    metric: str
    observed_value: float | None
    threshold_value: float | None
    unit: str
    contribution: float = 0.0     # peso 0..1 en el score de riesgo
    ref_id: int | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ref_table": self.ref_table,
            "ref_id": self.ref_id,
            "metric": self.metric,
            "observed_value": self.observed_value,
            "threshold_value": self.threshold_value,
            "unit": self.unit,
            "contribution": round(self.contribution, 3),
        }


@dataclass(slots=True)
class Criterion:
    """Una condición booleana del árbol, con su explicación para la UI."""

    name: str
    satisfied: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "OK " if self.satisfied else "FALLA"
        return f"[{mark}] {self.name}: {self.detail}"


@dataclass(slots=True)
class ActionDraft:
    """Acción del protocolo todavía sin ``id`` ni ``alert_id``."""

    sequence: int
    phase: str                    # AISLAMIENTO | DESINFECCION | CLINICO | LOGISTICA
    action_type: str
    description: str
    payload: dict[str, Any] = field(default_factory=dict)
    requires_human_confirmation: bool = True
    verification_type: str | None = None
    qr_code: str | None = None
    storage_location: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "phase": self.phase,
            "action_type": self.action_type,
            "description": self.description,
            "payload_json": json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
            "requires_human_confirmation": int(self.requires_human_confirmation),
            "verification_type": self.verification_type,
            "qr_code": self.qr_code,
            "storage_location": self.storage_location,
            "status": "PENDING",
        }


@dataclass(slots=True)
class RuleOutcome:
    """Resultado de evaluar una regla: disparó o no, con qué evidencia."""

    rule_id: str
    rule_version: str
    matched: bool
    severity: Severity
    category: str                 # ENV | MICRO | IMMUNO | INTEGRATED | SYSTEM
    title: str
    clinical_impact: str = ""
    route: Route | None = None
    scenario: Scenario | None = None
    risk_score: float = 0.0
    evidence: list[Evidence] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)
    actions: list[ActionDraft] = field(default_factory=list)
    dedup_suffix: str = ""
    notes: str = ""
    #: Subsistema ECLSS al que pertenece la señal: «AIR» o «WATER».
    #: Permite agrupar alarmas derivadas bajo el fallo primario correcto.
    subsystem: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    #: `rule_id` de la alerta raíz que la absorbe (supresión en cascada).
    suppressed_by: str | None = None

    @property
    def actions_for_payload(self) -> list[dict[str, Any]]:
        return [a.as_row() for a in self.actions]

    def summary(self) -> str:
        flag = "DISPARA" if self.matched else "  ok   "
        return f"{flag} {self.severity.value:<8} {self.rule_id:<34} {self.title}"


@dataclass(slots=True)
class EngineRun:
    """Resultado completo de una pasada del motor."""

    started_at: datetime
    finished_at: datetime
    window_start: datetime
    window_end: datetime
    outcomes: list[RuleOutcome] = field(default_factory=list)
    created_alert_ids: dict[str, int] = field(default_factory=dict)

    @property
    def actionable(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.matched and o.severity.is_actionable]

    @property
    def visible(self) -> list[RuleOutcome]:
        """Alertas que la UI debe mostrar (las suprimidas viven bajo su raíz)."""
        return [o for o in self.actionable if o.suppressed_by is None]

    @property
    def suppressed(self) -> list[RuleOutcome]:
        return [o for o in self.actionable if o.suppressed_by is not None]

    @property
    def latency_ms(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() * 1000.0

    def by_route(self) -> dict[Route, RuleOutcome]:
        return {o.route: o for o in self.outcomes if o.route is not None}

    def by_scenario(self) -> dict[Scenario, RuleOutcome]:
        return {o.scenario: o for o in self.outcomes if o.scenario is not None}


# =========================================================================== #
# 2. Analítica pura sobre series temporales (sin dependencias externas)
# =========================================================================== #


def stats(series: Sequence[tuple[datetime, float]]) -> SeriesStats:
    """
    Media, extremos y percentiles de una serie.

    Los percentiles existen por una razón concreta: clasificar con el máximo
    absoluto hace que un único pico de ruido de una muestra (un portazo, un
    transitorio del sensor) dispare una alerta crítica y entrene a la tripulación
    a ignorar el rojo. La especificación habla de niveles «continuos» y
    «sostenidos», así que se clasifica con p95 (y p05 por el lado bajo) y se
    reserva el máximo para los canales cuyo umbral es explícitamente de pico.
    """
    if not series:
        return SeriesStats()
    values = [v for _, v in series]
    ordered = sorted(values)
    n = len(ordered)

    def percentile(q: float) -> float:
        index = min(n - 1, max(0, int(round(q * (n - 1)))))
        return ordered[index]

    return SeriesStats(
        n=n,
        mean=sum(values) / n,
        vmin=ordered[0],
        vmax=ordered[-1],
        last=values[-1],
        p05=percentile(0.05),
        p95=percentile(0.95),
    )


def latest(series: Sequence[tuple[datetime, float]]) -> float | None:
    return series[-1][1] if series else None


def in_hours(series: Sequence[tuple[datetime, float]], lo: int, hi: int) -> Series:
    """Filtra muestras cuya hora local cae en ``[lo, hi)``."""
    return [(t, v) for t, v in series if lo <= t.hour < hi]


def is_night_ts(ts: datetime, cfg: EngineConfig) -> bool:
    return config.is_night(ts + timedelta(hours=cfg.habitat_utc_offset_hours), cfg)


def night_only(series: Sequence[tuple[datetime, float]], cfg: EngineConfig) -> Series:
    return [(t, v) for t, v in series if is_night_ts(t, cfg)]


def day_only(series: Sequence[tuple[datetime, float]], cfg: EngineConfig) -> Series:
    return [(t, v) for t, v in series if not is_night_ts(t, cfg)]


def night_key(ts: datetime, cfg: EngineConfig) -> date:
    """Identificador de «noche»: las horas 00–12 pertenecen a la noche previa."""
    local = ts + timedelta(hours=cfg.habitat_utc_offset_hours)
    return local.date() if local.hour >= 12 else (local - timedelta(days=1)).date()


def hours_over(
    series: Sequence[tuple[datetime, float]], threshold: float, max_gap_h: float = 1.0
) -> float:
    """
    Horas acumuladas con el valor por encima del umbral.

    Se integra por intervalos entre muestras consecutivas (no por conteo), con
    un tope ``max_gap_h`` para que un hueco de telemetría no infle la duración.
    """
    total = 0.0
    for (t0, _v0), (t1, v1) in zip(series, series[1:]):
        if v1 > threshold:
            gap_h = (t1 - t0).total_seconds() / 3600.0
            total += min(gap_h, max_gap_h)
    return total


def nights_over(
    series: Sequence[tuple[datetime, float]], threshold: float, cfg: EngineConfig
) -> list[date]:
    """Noches distintas en las que al menos una muestra superó el umbral."""
    nights: dict[date, float] = {}
    for ts, value in series:
        if not is_night_ts(ts, cfg):
            continue
        key = night_key(ts, cfg)
        nights[key] = max(nights.get(key, -math.inf), value)
    return sorted(k for k, v in nights.items() if v > threshold)


def consecutive_days(days: Sequence[date]) -> int:
    """Longitud de la racha más larga de días consecutivos."""
    best = run = 0
    previous: date | None = None
    for d in sorted(set(days)):
        run = run + 1 if previous is not None and (d - previous).days == 1 else 1
        best = max(best, run)
        previous = d
    return best


def leq(series: Sequence[tuple[datetime, float]]) -> float | None:
    """
    Nivel sonoro continuo equivalente (Leq) de un conjunto de muestras.

    Promedio energético: NO se puede promediar dBA en escala lineal. Para el
    MVP se asume que cada muestra ya es una media de 1 min con ponderación A.
    """
    if not series:
        return None
    energy = sum(10.0 ** (v / 10.0) for _, v in series) / len(series)
    return 10.0 * math.log10(energy)


def pct_change(baseline: float | None, current: float | None) -> float | None:
    """Caída relativa positiva: 0.45 = 45 % por debajo de la línea base."""
    if not baseline or current is None or baseline <= 0:
        return None
    return (baseline - current) / baseline


def viral_copies(series: Sequence[tuple[datetime, float]]) -> float | None:
    """Carga viral de referencia: máximo de la ventana (una PCR es puntual)."""
    return max((v for _, v in series), default=None)


# =========================================================================== #
# 3. Snapshot de evidencia
# =========================================================================== #


#: Canal de sensor → KPI ambiental que evalúa.
KIND_TO_KPI: dict[SensorKind, str] = {
    SensorKind.PRESSURE_TOTAL: "PRESSURE_TOTAL",
    SensorKind.PPO2: "PPO2",
    SensorKind.PPCO2: "PPCO2",
    SensorKind.TEMPERATURE: "TEMPERATURE",
    SensorKind.HUMIDITY: "HUMIDITY",
    SensorKind.NOISE_LEQ: "NOISE_LEQ",
    SensorKind.NOISE_PEAK: "NOISE_PEAK",
    SensorKind.VOC_TVOC: "VOC_TVOC",
}

PRIMARY_MODULE = "HAB-01"


@dataclass(slots=True)
class Snapshot:
    """Todas las señales que el motor necesita, ya recortadas por ventana."""

    start: datetime
    end: datetime
    env: dict[str, Series] = field(default_factory=dict)          # módulo primario
    env_by_module: dict[str, dict[str, Series]] = field(default_factory=dict)
    air: list[dict[str, Any]] = field(default_factory=list)
    swabs: list[dict[str, Any]] = field(default_factory=list)
    water: list[dict[str, Any]] = field(default_factory=list)
    crew: list[CrewRecord] = field(default_factory=list)
    bio: dict[str, dict[str, Series]] = field(default_factory=dict)  # crew → analito
    symptoms: dict[str, set[str]] = field(default_factory=dict)
    noise_history: Series = field(default_factory=list)           # lookback extendido
    cortisol_days_below: dict[str, list[date]] = field(default_factory=dict)

    def bio_latest(self, crew: str, analyte: str) -> float | None:
        return latest(self.bio.get(crew, {}).get(analyte, []))


# =========================================================================== #
# 4. Motor
# =========================================================================== #


class CorrelationEngine:
    """
    Evalúa el árbol de decisión completo y persiste las alertas resultantes.

    Parameters
    ----------
    repo:
        Repositorio ya sembrado (misión, módulos, tripulación, sensores).
    cfg:
        Configuración del motor; los valores por omisión reproducen la
        especificación funcional.
    """

    RULE_VERSION = config.DEFAULT_ENGINE_CONFIG.rule_version

    def __init__(
        self,
        repo: Repository,
        cfg: EngineConfig = config.DEFAULT_ENGINE_CONFIG,
    ) -> None:
        self.repo = repo
        self.cfg = cfg
        self._mission_id = repo.mission_id()

    # -- 4.1 lectura ------------------------------------------------------

    def build_snapshot(self, start: datetime, end: datetime) -> Snapshot:
        """Carga todas las señales de la ventana en una sola pasada."""
        lookback_start = min(start, end - timedelta(hours=max(72, self.cfg.window_hours)))

        env_by_module: dict[str, dict[str, Series]] = {}
        for module in self.repo.modules():
            channels: dict[str, Series] = {}
            for kind, _unit, _hz, _label in config.SENSOR_SPECS:
                series = self.repo.env_series(kind, start, end, module_code=module.code)
                if not series:
                    continue
                kpi_key = KIND_TO_KPI.get(kind)
                if kpi_key:
                    # Un mismo canal puede alimentar dos KPI según el momento
                    # del día: la iluminación diurna y la nocturna se juzgan
                    # contra cortes distintos.
                    channels[kpi_key] = series
                if kind is SensorKind.LIGHT_TOTAL:
                    channels["LIGHT_DAY"] = day_only(series, self.cfg)
                    channels["LIGHT_NIGHT"] = night_only(series, self.cfg)
                if kind is SensorKind.LIGHT_BLUE_480:
                    channels["BLUE_LIGHT_NIGHT"] = night_only(series, self.cfg)
                if kind is SensorKind.NOISE_LEQ:
                    channels["NOISE_NIGHT"] = night_only(series, self.cfg)
            env_by_module[module.code] = channels

        bio: dict[str, dict[str, Series]] = {}
        for analyte in Analyte:
            per_crew = self.repo.biomarker_series(analyte, lookback_start, end)
            for crew_code, series in per_crew.items():
                bio.setdefault(crew_code, {})[analyte.value] = series

        crew = self.repo.crew()
        symptoms = self.repo.symptoms(start, end)
        noise_history = self.repo.env_series(
            SensorKind.NOISE_LEQ,
            end - timedelta(hours=self.cfg.noise_lookback_hours),
            end,
            module_code=PRIMARY_MODULE,
        )

        snapshot = Snapshot(
            start=start,
            end=end,
            env=env_by_module.get(PRIMARY_MODULE, {}),
            env_by_module=env_by_module,
            air=self.repo.air_samples(start, end),
            swabs=self.repo.surface_swabs(start, end),
            water=self.repo.water_samples(start, end),
            crew=crew,
            bio=bio,
            symptoms=symptoms,
            noise_history=noise_history,
        )
        snapshot.cortisol_days_below = self._cortisol_days_below(snapshot)
        return snapshot

    def _cortisol_daily(
        self, snap: Snapshot
    ) -> dict[str, list[tuple[date, float, float, float]]]:
        """
        Curva diurna por tripulante: ``(día, AM, noche, ratio)``.

        El ratio se calcula **por día** y no promediando la ventana: promediar
        tres días oculta el aplanamiento del último día, que es justo el que
        describe el estado clínico actual. Este error de método haría que la
        Ruta 1 no disparase hasta 48 h tarde.
        """
        out: dict[str, list[tuple[date, float, float, float]]] = {}
        for member in snap.crew:
            series = snap.bio.get(member.code, {}).get(Analyte.CORTISOL.value, [])
            am: dict[date, list[float]] = {}
            pm: dict[date, list[float]] = {}
            for ts, value in series:
                (am if 6 <= ts.hour < 12 else pm).setdefault(ts.date(), []).append(value)
            days: list[tuple[date, float, float, float]] = []
            for day, am_values in sorted(am.items()):
                pm_values = pm.get(day) or pm.get(day + timedelta(days=1))
                if not pm_values:
                    continue
                am_mean = sum(am_values) / len(am_values)
                pm_mean = sum(pm_values) / len(pm_values)
                if pm_mean <= 0:
                    continue
                days.append((day, am_mean, pm_mean, am_mean / pm_mean))
            if days:
                out[member.code] = days
        return out

    def _cortisol_days_below(self, snap: Snapshot) -> dict[str, list[date]]:
        """
        Días con ratio cortisol mañana/noche por debajo del corte crítico.

        Necesario para la condición «aplanamiento durante 3 días consecutivos»,
        que no cabe en una ventana de 24 h.
        """
        out: dict[str, list[date]] = {}
        for crew_code, days in self._cortisol_daily(snap).items():
            bad = [d for d, _am, _pm, ratio in days if ratio < self.cfg.cortisol_ratio_crit]
            if bad:
                out[crew_code] = bad
        return out

    # -- 4.2 reglas atómicas ambientales ----------------------------------

    def _rule_env_kpi(self, snap: Snapshot, module_code: str, kpi_key: str,
                      series: Series) -> RuleOutcome:
        """
        Regla genérica: un canal ECLSS contra su umbral.

        Se evalúa la peor muestra de la ventana, porque en un entorno cerrado un
        pico de 4 min de `ppCO2` ya implica hipoxia tisular aunque la media del
        día sea correcta. La media y la duración fuera de rango acompañan como
        evidencia para el análisis de tendencia.
        """
        th = config.kpi(kpi_key)
        st = stats(series)

        # Canales cuyo riesgo está en el valor bajo (hipoxia, hipotensión).
        low_side = kpi_key in {"PPO2", "PRESSURE_TOTAL", "SPO2", "LYMPHOCYTE_ABS"}
        # Canales cuyo umbral es por definición de pico instantáneo.
        peak_side = kpi_key == "NOISE_PEAK"

        if low_side:
            observed = st.p05
        elif peak_side:
            observed = st.vmax
        else:
            observed = st.p95
        severity = th.classify(observed)

        rule_id = f"ENV_{kpi_key}_{module_code}"
        sev_label = "CRÍTICO" if severity is Severity.CRITICAL else "ADVERTENCIA"
        evidence = [
            Evidence(
                kind="ENV",
                ref_table="sensor_reading",
                metric=f"{kpi_key}:{module_code}",
                observed_value=observed,
                threshold_value=self._crossed_threshold(th, observed, severity),
                unit=th.unit,
                contribution=1.0 if severity is Severity.CRITICAL else 0.6,
            )
        ]
        if not st.empty:
            evidence.append(
                Evidence(
                    kind="ENV",
                    ref_table="sensor_reading",
                    metric=f"{kpi_key}:{module_code}:MEDIA",
                    observed_value=round(st.mean or 0.0, 4),
                    threshold_value=th.nominal_high or th.nominal_low,
                    unit=th.unit,
                    contribution=0.0,
                )
            )

        return RuleOutcome(
            rule_id=rule_id,
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="ENV",
            title=f"{kpi_key} {sev_label} EN {module_code}",
            clinical_impact=self._env_impact(kpi_key, severity),
            risk_score=100.0 if severity is Severity.CRITICAL else 45.0,
            evidence=evidence,
            dedup_suffix=module_code,
            notes=f"n={st.n} media={st.mean and round(st.mean, 3)} rango=[{st.vmin}, {st.vmax}]",
            window_start=snap.start,
            window_end=snap.end,
        )

    @staticmethod
    def _crossed_threshold(th: config.Threshold, observed: float | None,
                           severity: Severity) -> float | None:
        if observed is None:
            return None
        if severity is Severity.CRITICAL:
            return th.crit_high if (th.crit_high is not None and observed > th.crit_high) else th.crit_low
        if severity is Severity.WARNING:
            return th.warn_high if (th.warn_high is not None and observed > th.warn_high) else th.warn_low
        return th.warn_high or th.warn_low

    @staticmethod
    def _env_impact(kpi_key: str, severity: Severity) -> str:
        impacts = {
            "PPCO2": "Hipercapnia: caída de memoria de trabajo y cefalea en la tripulación.",
            "HUMIDITY": "Condensación en paneles fríos y proliferación fúngica en aire.",
            "PPO2": "Riesgo de hipoxia tisular o de atmósfera inflamable.",
            "PRESSURE_TOTAL": "Riesgo de hipoxia y enfermedad por descompresión.",
            "TEMPERATURE": "Estrés térmico y deterioro del sueño.",
            "NOISE_LEQ": "Fragmentación del sueño y activación del eje HPA.",
            "NOISE_PEAK": "Despertares abruptos y riesgo auditivo.",
            "VOC_TVOC": "Irritación de mucosas por mVOCs microbianos.",
            "LIGHT_DAY": "Desanclaje circadiano del ciclo sueño-vigilia.",
            "LIGHT_NIGHT": "Supresión melatonínica en fase de sueño.",
            "BLUE_LIGHT_NIGHT": "Supresión melatonínica y aplanamiento del cortisol.",
        }
        base = impacts.get(kpi_key, "Desviación de un parámetro de soporte vital.")
        prefix = "CRÍTICO: " if severity is Severity.CRITICAL else ""
        return prefix + base

    # -- 4.3 reglas microbiológicas ---------------------------------------

    def _rule_air_fungi(self, snap: Snapshot) -> RuleOutcome:
        cfg = self.cfg
        worst_sample = max(snap.air, key=lambda s: float(s["fungi_cfu_m3"]), default=None)
        genera: set[str] = set()
        for sample in snap.air:
            genera |= {g.upper() for g in json.loads(sample.get("dominant_fungi") or "[]")}

        value = float(worst_sample["fungi_cfu_m3"]) if worst_sample else None
        severity = config.kpi("AIR_FUNGI").classify(value)
        # Presencia de Aspergillus/Penicillium dispara alerta inmediata.
        escalation = bool(genera & config.MOLD_GENERA_ESCALATION)
        if escalation:
            severity = worst(severity, Severity.CRITICAL)

        evidence = []
        if worst_sample:
            evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="air_microbio_sample",
                    ref_id=worst_sample["id"],
                    metric=f"AIR_FUNGI:{worst_sample['module_code']}",
                    observed_value=value,
                    threshold_value=config.kpi("AIR_FUNGI").crit_high,
                    unit="UFC/m3",
                    contribution=1.0 if severity is Severity.CRITICAL else 0.6,
                )
            )
        if genera:
            evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="air_microbio_sample",
                    metric="GENEROS_FUNGICOS",
                    observed_value=float(len(genera)),
                    threshold_value=None,
                    unit="generos",
                    contribution=0.4 if escalation else 0.1,
                )
            )

        return RuleOutcome(
            rule_id="MICRO_AIR_FUNGI",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="MICRO",
            title="CARGA FÚNGICA AÉREA ELEVADA",
            clinical_impact="Riesgo de broncoespasmo, rinitis y neumonitis por hipersensibilidad.",
            risk_score=90.0 if escalation else (60.0 if severity is Severity.CRITICAL else 35.0),
            evidence=evidence,
            dedup_suffix="HAB",
            notes=f"géneros={sorted(genera)} muestras={len(snap.air)}",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_air_bacteria(self, snap: Snapshot) -> RuleOutcome:
        worst_sample = max(snap.air, key=lambda s: float(s["bacteria_cfu_m3"]), default=None)
        value = float(worst_sample["bacteria_cfu_m3"]) if worst_sample else None
        severity = config.kpi("AIR_BACTERIA").classify(value)
        evidence = []
        if worst_sample:
            evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="air_microbio_sample",
                    ref_id=worst_sample["id"],
                    metric=f"AIR_BACTERIA:{worst_sample['module_code']}",
                    observed_value=value,
                    threshold_value=config.kpi("AIR_BACTERIA").crit_high,
                    unit="UFC/m3",
                    contribution=1.0,
                )
            )
        return RuleOutcome(
            rule_id="MICRO_AIR_BACTERIA",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="MICRO",
            title="BIOAEROSOL BACTERIANO ELEVADO",
            clinical_impact="Mayor probabilidad de infección respiratoria en espacio confinado.",
            risk_score=55.0 if severity is Severity.CRITICAL else 30.0,
            evidence=evidence,
            dedup_suffix="HAB",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_surfaces(self, snap: Snapshot) -> list[RuleOutcome]:
        """Biocarga en superficies críticas + patógenos oportunistas (MinION)."""
        outcomes: list[RuleOutcome] = []

        worst_sample = max(
            snap.swabs, key=lambda s: float(s["total_cfu_per_100cm2"]), default=None
        )
        value = float(worst_sample["total_cfu_per_100cm2"]) if worst_sample else None
        severity = config.kpi("SURFACE_BIOLOAD").classify(value)
        evidence: list[Evidence] = []
        if worst_sample:
            evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="surface_swab",
                    ref_id=worst_sample["id"],
                    metric=f"SURFACE_BIOLOAD:{worst_sample['surface_label']}",
                    observed_value=value,
                    threshold_value=config.kpi("SURFACE_BIOLOAD").crit_high,
                    unit="UFC/100cm2",
                    contribution=0.8,
                )
            )
        outcomes.append(
            RuleOutcome(
                rule_id="MICRO_SURFACE_BIOLOAD",
                rule_version=self.RULE_VERSION,
                matched=severity.is_actionable,
                severity=severity,
                category="MICRO",
                title="BIOCARGA ALTA EN SUPERFICIE CRÍTICA",
                clinical_impact="Fuente de contacto directo para infecciones cutáneas y digestivas.",
                risk_score=50.0 if severity is Severity.CRITICAL else 25.0,
                evidence=evidence,
                dedup_suffix="HAB",
                window_start=snap.start,
                window_end=snap.end,
            )
        )

        # Patógenos oportunistas: el hallazgo por sí solo es crítico.
        hits: list[tuple[dict[str, Any], str]] = []
        for sample in snap.swabs:
            pathogens = {p.upper() for p in json.loads(sample.get("minion_pathogens") or "[]")}
            if sample.get("mrsa"):
                pathogens.add("MRSA")
            if sample.get("pseudomonas_aeruginosa"):
                pathogens.add("PSEUDOMONAS_AERUGINOSA")
            if sample.get("staphylococcus_aureus"):
                pathogens.add("STAPHYLOCOCCUS_AUREUS")
            for p in sorted(pathogens & config.OPPORTUNISTIC_PATHOGENS):
                hits.append((sample, p))

        outcomes.append(
            RuleOutcome(
                rule_id="MICRO_OPPORTUNISTIC_PATHOGEN",
                rule_version=self.RULE_VERSION,
                matched=bool(hits),
                severity=Severity.CRITICAL if hits else Severity.OK,
                category="MICRO",
                title="PATÓGENO OPORTUNISTA EN SUPERFICIE",
                clinical_impact="Riesgo de infección difícil de tratar con el botiquín de a bordo.",
                risk_score=85.0 if hits else 0.0,
                evidence=[
                    Evidence(
                        kind="MICRO",
                        ref_table="surface_swab",
                        ref_id=sample["id"],
                        metric=f"MINION:{pathogen}",
                        observed_value=1.0,
                        threshold_value=0.0,
                        unit="deteccion",
                        contribution=0.9,
                    )
                    for sample, pathogen in hits
                ],
                dedup_suffix="HAB",
                notes=f"{len(hits)} detecciones",
                window_start=snap.start,
                window_end=snap.end,
            )
        )
        return outcomes

    def _rule_water(self, snap: Snapshot) -> list[RuleOutcome]:
        """
        Sistema de recuperación de agua (WRS): dos reglas independientes.

        Se separan TOC y carga bacteriana a propósito: son dos alarmas con
        significados distintos (indicador temprano de biopelícula frente a
        contaminación establecida) y el operador necesita saber cuál de las dos
        disparó antes de cerrar una válvula. Ambas convergen en la alerta de
        sistema si se confirman juntas.
        """
        outcomes: list[RuleOutcome] = []

        # -- (a) Carbono orgánico total: precursor de biopelícula ----------
        toc_sample = max(snap.water, key=lambda s: float(s["toc_mg_l"]), default=None)
        toc = float(toc_sample["toc_mg_l"]) if toc_sample else None
        toc_sev = config.kpi("WATER_TOC").classify(toc)
        outcomes.append(
            RuleOutcome(
                rule_id="MICRO_WATER_TOC",
                rule_version=self.RULE_VERSION,
                matched=toc_sev.is_actionable,
                severity=toc_sev,
                category="MICRO",
                title="TOC ELEVADO EN EL WRS",
                clinical_impact="Biopelícula incipiente en las líneas de agua reciclada.",
                risk_score=70.0 if toc_sev is Severity.CRITICAL else 35.0,
                evidence=(
                    [
                        Evidence(
                            kind="MICRO",
                            ref_table="water_quality_sample",
                            ref_id=toc_sample["id"],
                            metric=f"WATER_TOC:{toc_sample['circuit']}",
                            observed_value=toc,
                            threshold_value=config.kpi("WATER_TOC").crit_high,
                            unit="mg/L",
                            contribution=1.0 if toc_sev is Severity.CRITICAL else 0.6,
                        )
                    ]
                    if toc_sample
                    else []
                ),
                dedup_suffix="WRS",
                notes=f"muestras={len(snap.water)}",
                window_start=snap.start,
                window_end=snap.end,
            )
        )

        # -- (b) Carga bacteriana y coliformes ----------------------------
        bact_sample = max(
            snap.water, key=lambda s: float(s["total_bacteria_cfu_ml"]), default=None
        )
        bact = float(bact_sample["total_bacteria_cfu_ml"]) if bact_sample else None
        bact_sev = config.kpi("WATER_BACTERIA").classify(bact)
        coliforms = any(int(s.get("coliforms_present") or 0) == 1 for s in snap.water)
        if coliforms:
            bact_sev = Severity.CRITICAL

        bact_evidence: list[Evidence] = []
        if bact_sample:
            bact_evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="water_quality_sample",
                    ref_id=bact_sample["id"],
                    metric=f"WATER_BACTERIA:{bact_sample['circuit']}",
                    observed_value=bact,
                    threshold_value=config.kpi("WATER_BACTERIA").crit_high,
                    unit="UFC/mL",
                    contribution=1.0 if bact_sev is Severity.CRITICAL else 0.6,
                )
            )
        if coliforms:
            bact_evidence.append(
                Evidence(
                    kind="MICRO",
                    ref_table="water_quality_sample",
                    metric="COLIFORMES",
                    observed_value=1.0,
                    threshold_value=0.0,
                    unit="presencia",
                    contribution=1.0,
                )
            )

        outcomes.append(
            RuleOutcome(
                rule_id="MICRO_WATER_BACTERIA",
                rule_version=self.RULE_VERSION,
                matched=bact_sev.is_actionable,
                severity=bact_sev,
                category="MICRO",
                title="BACTERIAS EN AGUA POTABLE",
                clinical_impact=(
                    "Exposición digestiva y dérmica; riesgo de ITU y gastroenteritis "
                    "infecciosa con botiquín limitado."
                ),
                risk_score=80.0 if bact_sev is Severity.CRITICAL else 40.0,
                evidence=bact_evidence,
                dedup_suffix="WRS",
                notes=f"coliformes={coliforms}",
                window_start=snap.start,
                window_end=snap.end,
            )
        )
        return outcomes

    # -- 4.4 reglas de biomarcadores --------------------------------------

    @staticmethod
    def _siga_threshold(snap: Snapshot, crew_code: str) -> float | None:
        """Corte individual de sIgA: línea base menos la caída crítica."""
        for member in snap.crew:
            if member.code == crew_code:
                return round(member.baseline_siga_ug_ml * (1 - config.SIGA_DROP_CRIT), 1)
        return None

    def _latest_cortisol_ratio(self, snap: Snapshot, crew_code: str) -> float | None:
        """Ratio AM/NOCHE del último día con curva completa (evidencia citable)."""
        days = self._cortisol_daily(snap).get(crew_code) or []
        return round(days[-1][3], 3) if days else None

    def _rule_siga(self, snap: Snapshot) -> RuleOutcome:
        """Caída de sIgA respecto a la línea base pre-vuelo (barrera mucosa)."""
        evidence: list[Evidence] = []
        severity = Severity.OK
        affected: list[str] = []
        for member in snap.crew:
            series = snap.bio.get(member.code, {}).get(Analyte.SIGA.value, [])
            current = latest(series)
            drop = pct_change(member.baseline_siga_ug_ml, current)
            if drop is None:
                continue
            if drop >= config.SIGA_DROP_CRIT:
                member_sev = Severity.CRITICAL
            elif drop >= config.SIGA_DROP_WARN:
                member_sev = Severity.WARNING
            else:
                member_sev = Severity.OK
            severity = worst(severity, member_sev)
            if member_sev.is_actionable:
                affected.append(member.code)
                evidence.append(
                    Evidence(
                        kind="BIOMARKER",
                        ref_table="biomarker_result",
                        metric=f"SIGA:{member.code}",
                        observed_value=current,
                        threshold_value=round(
                            member.baseline_siga_ug_ml * (1 - config.SIGA_DROP_CRIT), 1
                        ),
                        unit="ug/mL",
                        contribution=min(1.0, (drop or 0.0) / config.SIGA_DROP_CRIT),
                    )
                )

        return RuleOutcome(
            rule_id="BIO_SIGA_DROP",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="IMMUNO",
            title=f"sIgA EN CAÍDA ({len(affected)} TRIPULANTES)",
            clinical_impact="Vulnerabilidad de la barrera mucosa: puerta de entrada a infecciones oportunistas.",
            risk_score=70.0 if severity is Severity.CRITICAL else 35.0,
            evidence=evidence,
            dedup_suffix="CREW",
            notes=f"afectados={affected}",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_cortisol(self, snap: Snapshot) -> RuleOutcome:
        """
        Curva diurna de cortisol: ratio mañana/noche y cortisol nocturno.

        Se evalúa el **último día completo** de cada tripulante (no la media de
        la ventana): el aplanamiento es un fenómeno del día en curso y promediar
        con días previos normales lo diluiría hasta hacerlo invisible.
        """
        daily = self._cortisol_daily(snap)
        evidence: list[Evidence] = []
        severity = Severity.OK
        affected: list[str] = []

        for member in snap.crew:
            days = daily.get(member.code)
            if not days:
                continue
            day, am, night, ratio = days[-1]
            consecutive = consecutive_days(
                [d for d, _a, _p, r in days if r < self.cfg.cortisol_ratio_crit]
            )
            member_sev = Severity.OK
            if ratio < self.cfg.cortisol_ratio_crit or night > self.cfg.cortisol_night_crit:
                member_sev = Severity.CRITICAL
            elif ratio < config.CORTISOL_RATIO_WARN:
                member_sev = Severity.WARNING
            severity = worst(severity, member_sev)
            if member_sev.is_actionable:
                affected.append(member.code)
                evidence.append(
                    Evidence(
                        kind="BIOMARKER",
                        ref_table="biomarker_result",
                        metric=f"CORTISOL_RATIO:{member.code}",
                        observed_value=round(ratio, 3),
                        threshold_value=self.cfg.cortisol_ratio_crit,
                        unit="ratio AM/NOCHE",
                        contribution=1.0 if member_sev is Severity.CRITICAL else 0.5,
                    )
                )
            if consecutive:
                evidence.append(
                    Evidence(
                        kind="BIOMARKER",
                        ref_table="biomarker_result",
                        metric=f"CORTISOL_DIAS_APLANADOS:{member.code}",
                        observed_value=float(consecutive),
                        threshold_value=3.0,
                        unit="días",
                        contribution=0.4,
                    )
                )

        return RuleOutcome(
            rule_id="BIO_CORTISOL_FLATTENING",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="IMMUNO",
            title=f"CURVA DE CORTISOL APLANADA ({len(affected)})",
            clinical_impact="Hiperactividad del eje HPA: supresión Th1 y menor vigilancia inmune.",
            risk_score=65.0 if severity is Severity.CRITICAL else 35.0,
            evidence=evidence,
            dedup_suffix="CREW",
            notes=f"afectados={affected}",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_viral(self, snap: Snapshot) -> RuleOutcome:
        """Reactivación de herpesvirus latentes (EBV, CMV, VZV) por qPCR."""
        assays = (Analyte.EBV_DNA, Analyte.CMV_DNA, Analyte.VZV_DNA)
        evidence: list[Evidence] = []
        severity = Severity.OK
        affected: list[tuple[str, str, float]] = []
        for member in snap.crew:
            for assay in assays:
                copies = viral_copies(snap.bio.get(member.code, {}).get(assay.value, []))
                if copies is None:
                    continue
                if copies >= self.cfg.viral_copies_crit:
                    member_sev = Severity.CRITICAL
                elif copies >= config.VIRAL_WARN_COPIES_ML:
                    member_sev = Severity.WARNING
                else:
                    member_sev = Severity.OK
                severity = worst(severity, member_sev)
                if member_sev.is_actionable:
                    affected.append((member.code, assay.value, copies))
                    evidence.append(
                        Evidence(
                            kind="BIOMARKER",
                            ref_table="biomarker_result",
                            metric=f"{assay.value}:{member.code}",
                            observed_value=copies,
                            threshold_value=self.cfg.viral_copies_crit,
                            unit="copias/mL",
                            contribution=1.0 if member_sev is Severity.CRITICAL else 0.4,
                        )
                    )

        return RuleOutcome(
            rule_id="BIO_VIRAL_REACTIVATION",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="IMMUNO",
            title=f"REACTIVACIÓN VIRAL ({len(affected)})",
            clinical_impact="Riesgo de herpes zóster, mononucleosis atípica o encefalitis.",
            risk_score=75.0 if severity is Severity.CRITICAL else 40.0,
            evidence=evidence,
            dedup_suffix="CREW",
            notes=f"detecciones={affected}",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_cytokines(self, snap: Snapshot) -> RuleOutcome:
        """
        Panel de citoquinas: inflamación de bajo grado y desvío Th1→Th2.

        Se evalúan tres señales: IL-6 elevada, TNF-α elevado y la relación
        IFN-γ/IL-10 (con umbral operativo, ver `config`).
        """
        evidence: list[Evidence] = []
        severity = Severity.OK
        il6_flagged = 0
        for member in snap.crew:
            il6 = snap.bio_latest(member.code, Analyte.IL6.value)
            if il6 is not None:
                il6_sev = config.kpi("IL6").classify(il6)
                severity = worst(severity, il6_sev)
                if il6 > self.cfg.il6_crit_pg_ml:
                    il6_flagged += 1
                # Sólo se cita como evidencia al tripulante cuyo valor sostiene
                # la alerta: listar a los sanos convierte la evidencia en ruido.
                if il6_sev.is_actionable:
                    evidence.append(
                        Evidence(
                            kind="BIOMARKER",
                            ref_table="biomarker_result",
                            metric=f"IL6:{member.code}",
                            observed_value=il6,
                            threshold_value=self.cfg.il6_crit_pg_ml,
                            unit="pg/mL",
                            contribution=min(1.0, il6 / 10.0),
                        )
                    )

            tnf = snap.bio_latest(member.code, Analyte.TNF_A.value)
            if tnf is not None:
                tnf_sev = config.kpi("TNF_A").classify(tnf)
                severity = worst(severity, tnf_sev)
                if tnf_sev.is_actionable:
                    evidence.append(
                        Evidence(
                            kind="BIOMARKER",
                            ref_table="biomarker_result",
                            metric=f"TNF_A:{member.code}",
                            observed_value=tnf,
                            threshold_value=config.kpi("TNF_A").crit_high,
                            unit="pg/mL",
                            contribution=0.5,
                        )
                    )

            ifng = snap.bio_latest(member.code, Analyte.IFN_G.value)
            il10 = snap.bio_latest(member.code, Analyte.IL10.value)
            if ifng is not None and il10:
                ratio = ifng / il10
                if ratio < config.IFNG_IL10_CRIT:
                    severity = worst(severity, Severity.CRITICAL)
                elif ratio < config.IFNG_IL10_WARN:
                    severity = worst(severity, Severity.WARNING)
                if ratio < config.IFNG_IL10_WARN:
                    evidence.append(
                        Evidence(
                            kind="BIOMARKER",
                            ref_table="biomarker_result",
                            metric=f"IFNG_IL10:{member.code}",
                            observed_value=round(ratio, 3),
                            threshold_value=config.IFNG_IL10_NOMINAL_MIN,
                            unit="ratio",
                            contribution=0.7,
                        )
                    )

        return RuleOutcome(
            rule_id="BIO_CYTOKINE_SHIFT",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="IMMUNO",
            title=f"PERFIL INFLAMATORIO Th2 ({il6_flagged} CON IL-6)",
            clinical_impact="Inflamación sistémica de bajo grado e inmunosenescencia acelerada.",
            risk_score=60.0 if severity is Severity.CRITICAL else 30.0,
            evidence=evidence,
            dedup_suffix="CREW",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _rule_differential(self, snap: Snapshot) -> RuleOutcome:
        """Hematología capilar: neutrofilia + linfopenia, o eosinofilia."""
        evidence: list[Evidence] = []
        severity = Severity.OK
        affected: list[str] = []

        for member in snap.crew:
            neutrophils = snap.bio_latest(member.code, Analyte.NEUTROPHIL_PCT.value)
            lymphocytes = snap.bio_latest(member.code, Analyte.LYMPHOCYTE_ABS.value)
            eosinophils = snap.bio_latest(member.code, Analyte.EOSINOPHIL_ABS.value)

            if (
                neutrophils is not None
                and lymphocytes is not None
                and neutrophils > 75.0
                and lymphocytes < 1000.0
            ):
                severity = worst(severity, Severity.CRITICAL)
                affected.append(member.code)
                evidence.append(
                    Evidence(
                        kind="BIOMARKER",
                        ref_table="biomarker_result",
                        metric=f"NEUTROFILIA+LYMPHOPENIA:{member.code}",
                        observed_value=neutrophils,
                        threshold_value=75.0,
                        unit="% neutrófilos",
                        contribution=0.9,
                    )
                )

            if eosinophils is not None:
                eos_sev = config.kpi("EOSINOPHIL_ABS").classify(eosinophils)
                severity = worst(severity, eos_sev)
                if eos_sev.is_actionable:
                    affected.append(member.code)
                    evidence.append(
                        Evidence(
                            kind="BIOMARKER",
                            ref_table="biomarker_result",
                            metric=f"EOSINOFILOS:{member.code}",
                            observed_value=eosinophils,
                            threshold_value=self.cfg.eosinophil_crit,
                            unit="cel/uL",
                            contribution=1.0 if eos_sev is Severity.CRITICAL else 0.5,
                        )
                    )

        return RuleOutcome(
            rule_id="BIO_DIFFERENTIAL_SHIFT",
            rule_version=self.RULE_VERSION,
            matched=severity.is_actionable,
            severity=severity,
            category="IMMUNO",
            title="ALTERACIÓN DEL LEUCOGRAMA",
            clinical_impact="Neutrofilia con linfopenia (infección) o eosinofilia (hipersensibilidad).",
            risk_score=70.0 if severity is Severity.CRITICAL else 30.0,
            evidence=evidence,
            dedup_suffix="CREW",
            notes=f"afectados={sorted(set(affected))}",
            window_start=snap.start,
            window_end=snap.end,
        )

    # -- 4.5 rutas fisiopatológicas ---------------------------------------

    def _route_r1(self, snap: Snapshot) -> RuleOutcome:
        """
        Ruta 1 — Desajuste circadiano + estrés acústico → reactivación viral.

        Disparador ambiental (ruido nocturno o luz azul) → respuesta
        neuroendocrina (cortisol aplanado) → desregulación inmune (caída de
        vigilancia Th1/NK) → ADN viral detectable en saliva.
        """
        cfg = self.cfg
        noise_nights = nights_over(snap.noise_history, cfg.noise_night_dba, cfg)
        blue_night_max = stats(snap.env.get("BLUE_LIGHT_NIGHT", [])).vmax
        noise_night_leq = leq(night_only(snap.noise_history, cfg))

        env_ok = (
            consecutive_days(noise_nights) >= cfg.noise_nights_required
            or (blue_night_max is not None and blue_night_max > cfg.blue_light_night_lux)
        )

        cortisol_affected = [
            m.code
            for m in snap.crew
            if len(snap.cortisol_days_below.get(m.code, [])) >= 1
            or any(
                v > cfg.cortisol_night_crit
                for t, v in snap.bio.get(m.code, {}).get(Analyte.CORTISOL.value, [])
                if not (6 <= t.hour < 12)
            )
        ]

        viral_affected = [
            (m.code, assay.value, copies)
            for m in snap.crew
            for assay in (Analyte.EBV_DNA, Analyte.CMV_DNA, Analyte.VZV_DNA)
            if (copies := viral_copies(snap.bio.get(m.code, {}).get(assay.value, []))) is not None
            and copies >= cfg.viral_copies_crit
        ]

        criteria = [
            Criterion(
                "Disparador ambiental (3 noches > 62 dBA o luz azul > 50 lux)",
                env_ok,
                f"noches={len(noise_nights)} racha={consecutive_days(noise_nights)} "
                f"Leq_nocturno={noise_night_leq and round(noise_night_leq, 1)} dBA "
                f"azul_max={blue_night_max}",
            ),
            Criterion(
                "Respuesta HPA (ratio cortisol AM/NOCHE < 2.0)",
                bool(cortisol_affected),
                f"afectados={cortisol_affected}",
            ),
            Criterion(
                "Reactivación viral (>= 1e4 copias/mL)", bool(viral_affected),
                f"detecciones={[(c, a) for c, a, _ in viral_affected]}",
            ),
        ]
        matched = all(c.satisfied for c in criteria)
        satisfied = sum(c.satisfied for c in criteria)

        evidence = [
            Evidence(
                kind="ENV",
                ref_table="sensor_reading",
                metric="NOISE_NIGHT_LEQ",
                observed_value=round(noise_night_leq, 1) if noise_night_leq else None,
                threshold_value=cfg.noise_night_dba,
                unit="dBA",
                contribution=1 / 3,
            ),
            Evidence(
                kind="ENV",
                ref_table="sensor_reading",
                metric="BLUE_LIGHT_NIGHT",
                observed_value=blue_night_max,
                threshold_value=cfg.blue_light_night_lux,
                unit="lux@480nm",
                contribution=1 / 3,
            ),
        ]
        evidence += [
            Evidence(
                kind="BIOMARKER",
                ref_table="biomarker_result",
                metric=f"VIRAL:{crew}:{assay}",
                observed_value=copies,
                threshold_value=cfg.viral_copies_crit,
                unit="copias/mL",
                contribution=1 / 3,
            )
            for crew, assay, copies in viral_affected
        ]
        evidence += [
            Evidence(
                kind="BIOMARKER",
                ref_table="biomarker_result",
                metric=f"CORTISOL_RATIO:{crew}",
                observed_value=self._latest_cortisol_ratio(snap, crew),
                threshold_value=cfg.cortisol_ratio_crit,
                unit="ratio",
                contribution=1 / 3,
            )
            for crew in cortisol_affected
        ]

        return RuleOutcome(
            rule_id="ROUTE_R1_CIRCADIAN_VIRAL",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else (Severity.WARNING if satisfied >= 2 else Severity.OK),
            category="INTEGRATED",
            route=Route.R1_CIRCADIAN_VIRAL,
            title="RUTA 1: CIRCADIANO → VIRAL",
            clinical_impact="Pérdida de vigilancia inmune sobre herpesvirus latentes: prever lesiones en 48–72 h.",
            risk_score=round(100 * satisfied / 3, 1),
            evidence=evidence,
            criteria=criteria,
            dedup_suffix="R1",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _route_r2(self, snap: Snapshot) -> RuleOutcome:
        """
        Ruta 2 — Humedad alta + fallo de filtración → hipersensibilidad.

        Disparador ambiental (HR > 70 % sostenida, filtros saturados) →
        proliferación fúngica y mVOCs → desvío Th1→Th2 con degranulación de
        mastocitos → rinitis, dermatitis o broncoespasmo en tripulantes sin
        antecedentes alérgicos terrestres.
        """
        cfg = self.cfg
        humidity_series = snap.env.get("HUMIDITY", [])
        sustained_h = hours_over(humidity_series, cfg.humidity_sustained_pct)
        humidity_ok = sustained_h >= cfg.humidity_sustained_hours

        fungi_max = max((float(s["fungi_cfu_m3"]) for s in snap.air), default=None)
        fungi_ok = fungi_max is not None and fungi_max > cfg.fungi_crit_cfu_m3
        mvocs_max = max((s.get("mvocs_ppb") or 0.0 for s in snap.air), default=0.0)

        th2_affected: list[str] = []
        for member in snap.crew:
            eos = snap.bio_latest(member.code, Analyte.EOSINOPHIL_ABS.value)
            ige = snap.bio_latest(member.code, Analyte.IGE.value)
            if (eos is not None and eos > cfg.eosinophil_crit) or (
                ige is not None and ige > config.kpi("IGE").crit_high
            ):
                th2_affected.append(member.code)

        respiratory = [
            code for code, syms in snap.symptoms.items() if syms & {"WHEEZE", "DYSPNEA"}
        ]
        low_spo2 = [
            m.code
            for m in snap.crew
            if (spo2 := snap.bio_latest(m.code, Analyte.SPO2.value)) is not None
            and spo2 < config.kpi("SPO2").crit_low
        ]
        clinical_ok = bool(th2_affected or respiratory or low_spo2)

        criteria = [
            Criterion(
                f"HR > {cfg.humidity_sustained_pct:.0f} % durante > {cfg.humidity_sustained_hours:.0f} h",
                humidity_ok, f"acumulado={sustained_h:.1f} h",
            ),
            Criterion(
                f"Hongos en aire > {cfg.fungi_crit_cfu_m3:.0f} UFC/m3",
                fungi_ok, f"max={fungi_max} UFC/m3 mVOCs={mvocs_max} ppb",
            ),
            Criterion(
                "Respuesta Th2 o clínica (eosinofilia / IgE / sibilancias / SpO2)",
                clinical_ok,
                f"eos/IgE={th2_affected} respiratorio={respiratory} SpO2_bajo={low_spo2}",
            ),
        ]
        matched = humidity_ok and fungi_ok and clinical_ok
        satisfied = sum(c.satisfied for c in criteria)

        evidence = [
            Evidence(
                kind="ENV", ref_table="sensor_reading", metric="HUMIDITY_HORAS_SOBRE_70",
                observed_value=round(sustained_h, 1), threshold_value=cfg.humidity_sustained_hours,
                unit="h", contribution=1 / 3,
            ),
            Evidence(
                kind="MICRO", ref_table="air_microbio_sample", metric="AIR_FUNGI",
                observed_value=fungi_max, threshold_value=cfg.fungi_crit_cfu_m3,
                unit="UFC/m3", contribution=1 / 3,
            ),
        ]
        evidence += [
            Evidence(
                kind="BIOMARKER", ref_table="biomarker_result", metric=f"EOSINOFILOS:{c}",
                observed_value=snap.bio_latest(c, Analyte.EOSINOPHIL_ABS.value),
                threshold_value=cfg.eosinophil_crit, unit="cel/uL", contribution=1 / 3,
            )
            for c in th2_affected
        ]

        return RuleOutcome(
            rule_id="ROUTE_R2_HUMIDITY_ALLERGY",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else (Severity.WARNING if satisfied >= 2 else Severity.OK),
            category="INTEGRATED",
            route=Route.R2_HUMIDITY_ALLERGY,
            title="RUTA 2: HUMEDAD → ALERGIA",
            clinical_impact="Sensibilización a antígenos fúngicos: rinitis, dermatitis y broncoespasmo.",
            risk_score=round(100 * satisfied / 3, 1),
            evidence=evidence,
            criteria=criteria,
            dedup_suffix="R2",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _route_r3(self, snap: Snapshot) -> RuleOutcome:
        """
        Ruta 3 — Biopelícula en el WRS + microgravedad → infecciones oportunistas.

        Disparador (TOC > 5 mg/L) → selección de cepas virulentas → exposición
        digestiva/dérmica con barrera epitelial debilitada → fagocitosis
        defectuosa e IL-6 elevada → ITU, gastroenteritis o foliculitis
        resistentes al botiquín de a bordo.
        """
        cfg = self.cfg
        toc_max = max((float(s["toc_mg_l"]) for s in snap.water), default=None)
        bact_max = max((float(s["total_bacteria_cfu_ml"]) for s in snap.water), default=None)
        coliforms = any(int(s.get("coliforms_present") or 0) == 1 for s in snap.water)
        water_ok = bool(
            (toc_max is not None and toc_max > cfg.toc_crit_mg_l)
            or (bact_max is not None and bact_max > cfg.water_bacteria_crit)
            or coliforms
        )

        siga_affected = [
            m.code
            for m in snap.crew
            if (drop := pct_change(m.baseline_siga_ug_ml,
                                   snap.bio_latest(m.code, Analyte.SIGA.value))) is not None
            and drop >= config.SIGA_DROP_CRIT
        ]
        il6_affected = [
            m.code for m in snap.crew
            if (il6 := snap.bio_latest(m.code, Analyte.IL6.value)) is not None
            and il6 > cfg.il6_crit_pg_ml
        ]
        immune_ok = bool(siga_affected or len(il6_affected) >= cfg.il6_crew_required)

        criteria = [
            Criterion("Biopelícula en WRS (TOC > 5 mg/L o > 100 UFC/mL o coliformes)",
                      water_ok, f"TOC={toc_max} UFC/mL={bact_max} coliformes={coliforms}"),
            Criterion("Compromiso inmune (sIgA -40 % o IL-6 > 5 pg/mL en >= 2)",
                      immune_ok, f"sIgA={siga_affected} IL6={il6_affected}"),
        ]
        matched = all(c.satisfied for c in criteria)
        satisfied = sum(c.satisfied for c in criteria)

        evidence = [
            Evidence(kind="MICRO", ref_table="water_quality_sample", metric="WATER_TOC",
                     observed_value=toc_max, threshold_value=cfg.toc_crit_mg_l,
                     unit="mg/L", contribution=0.5),
            Evidence(kind="MICRO", ref_table="water_quality_sample", metric="WATER_BACTERIA",
                     observed_value=bact_max, threshold_value=cfg.water_bacteria_crit,
                     unit="UFC/mL", contribution=0.5),
        ]
        evidence += [
            Evidence(kind="BIOMARKER", ref_table="biomarker_result", metric=f"SIGA:{c}",
                     observed_value=snap.bio_latest(c, Analyte.SIGA.value),
                     threshold_value=self._siga_threshold(snap, c),
                     unit="ug/mL", contribution=0.5)
            for c in siga_affected
        ]

        return RuleOutcome(
            rule_id="ROUTE_R3_BIOFILM_INFECTION",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else (Severity.WARNING if satisfied == 1 else Severity.OK),
            category="INTEGRATED",
            route=Route.R3_BIOFILM_INFECTION,
            title="RUTA 3: BIOPELÍCULA → INFECCIÓN",
            clinical_impact="Infecciones oportunistas (ITU, gastroenteritis, foliculitis) resistentes al botiquín.",
            risk_score=round(100 * satisfied / 2, 1),
            evidence=evidence,
            criteria=criteria,
            dedup_suffix="R3",
            window_start=snap.start,
            window_end=snap.end,
        )

    # -- 4.6 escenarios de autonomía (árbol de decisión) ------------------

    def _scenario_a(self, snap: Snapshot) -> RuleOutcome:
        """
        Escenario A — Contaminación microbiana hídrica + compromiso inmune.

        ``(TOC > 5 OR bacterias > 100) AND (sIgA -40 % OR IL-6 > 5 en >= 2)``
        """
        cfg = self.cfg
        toc_max = max((float(s["toc_mg_l"]) for s in snap.water), default=None)
        bact_max = max((float(s["total_bacteria_cfu_ml"]) for s in snap.water), default=None)
        coliforms = any(int(s.get("coliforms_present") or 0) == 1 for s in snap.water)
        water_ok = bool(
            (toc_max is not None and toc_max > cfg.toc_crit_mg_l)
            or (bact_max is not None and bact_max > cfg.water_bacteria_crit)
            or coliforms
        )

        siga_affected = [
            m.code
            for m in snap.crew
            if (drop := pct_change(m.baseline_siga_ug_ml,
                                   snap.bio_latest(m.code, Analyte.SIGA.value))) is not None
            and drop >= cfg.siga_drop_crit
        ]
        il6_affected = [
            m.code for m in snap.crew
            if (il6 := snap.bio_latest(m.code, Analyte.IL6.value)) is not None
            and il6 > cfg.il6_crit_pg_ml
        ]
        immune_ok = bool(siga_affected or len(il6_affected) >= cfg.il6_crew_required)

        criteria = [
            Criterion("Agua fuera de rango (TOC > 5 mg/L o > 100 UFC/mL o coliformes)",
                      water_ok, f"TOC={toc_max} UFC/mL={bact_max} coliformes={coliforms}"),
            Criterion("Compromiso inmune (sIgA -40 % o IL-6 > 5 pg/mL en >= 2 sujetos)",
                      immune_ok, f"sIgA={siga_affected} IL6={il6_affected}"),
        ]
        matched = all(c.satisfied for c in criteria)
        affected = sorted(set(siga_affected) | set(il6_affected))

        evidence = [
            Evidence(kind="MICRO", ref_table="water_quality_sample", metric="WATER_TOC",
                     observed_value=toc_max, threshold_value=cfg.toc_crit_mg_l,
                     unit="mg/L", contribution=0.5),
            Evidence(kind="MICRO", ref_table="water_quality_sample", metric="WATER_BACTERIA",
                     observed_value=bact_max, threshold_value=cfg.water_bacteria_crit,
                     unit="UFC/mL", contribution=0.5),
        ]
        evidence += [
            Evidence(kind="BIOMARKER", ref_table="biomarker_result", metric=f"SIGA:{c}",
                     observed_value=snap.bio_latest(c, Analyte.SIGA.value),
                     threshold_value=self._siga_threshold(snap, c),
                     unit="ug/mL", contribution=0.5)
            for c in affected
        ]

        return RuleOutcome(
            rule_id="SCENARIO_A_WATER_IMMUNE",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else Severity.OK,
            category="INTEGRATED",
            scenario=Scenario.A_WATER_IMMUNE,
            title="CONTAMINACIÓN MICROBIANA DEL AGUA",
            clinical_impact=(
                f"Riesgo alto de ITU y gastroenteritis en {len(affected)} tripulante(s): "
                "retirar el circuito A de consumo."
            ) if matched else "",
            risk_score=100.0 if matched else 0.0,
            evidence=evidence if matched else [],
            criteria=criteria,
            actions=self._actions_scenario_a(affected) if matched else [],
            dedup_suffix="A",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _scenario_b(self, snap: Snapshot) -> RuleOutcome:
        """
        Escenario B — Disrupción circadiana/acústica + reactivación viral.

        ``(ruido nocturno > 62 dBA en >= 3 noches OR azul > 50 lux) AND
        (ratio cortisol < 2.0) AND (EBV o VZV > 1e4 en >= 1 tripulante)``
        """
        cfg = self.cfg
        noise_nights = nights_over(snap.noise_history, cfg.noise_night_dba, cfg)
        noise_ok = consecutive_days(noise_nights) >= cfg.noise_nights_required
        blue_max = stats(snap.env.get("BLUE_LIGHT_NIGHT", [])).vmax
        blue_ok = blue_max is not None and blue_max > cfg.blue_light_night_lux

        cortisol_affected = [
            m.code for m in snap.crew if snap.cortisol_days_below.get(m.code)
        ]
        viral_affected = [
            (m.code, assay.value, copies)
            for m in snap.crew
            for assay in (Analyte.EBV_DNA, Analyte.VZV_DNA)
            if (copies := viral_copies(snap.bio.get(m.code, {}).get(assay.value, []))) is not None
            and copies >= cfg.viral_copies_crit
        ]

        criteria = [
            Criterion("Ruido nocturno > 62 dBA en >= 3 noches o luz azul > 50 lux",
                      noise_ok or blue_ok,
                      f"racha_noches={consecutive_days(noise_nights)} azul_max={blue_max}"),
            Criterion("Ratio cortisol AM/NOCHE < 2.0", bool(cortisol_affected),
                      f"afectados={cortisol_affected}"),
            Criterion("VZV o EBV > 1e4 copias/mL", bool(viral_affected),
                      f"detecciones={[(c, a) for c, a, _ in viral_affected]}"),
        ]
        matched = all(c.satisfied for c in criteria)
        viral_zoster = [c for c, a, _ in viral_affected if a == Analyte.VZV_DNA.value]

        evidence = [
            Evidence(kind="ENV", ref_table="sensor_reading", metric="NOISE_NIGHTS_OVER_62",
                     observed_value=float(consecutive_days(noise_nights)),
                     threshold_value=float(cfg.noise_nights_required), unit="noches",
                     contribution=0.34),
            Evidence(kind="ENV", ref_table="sensor_reading", metric="BLUE_LIGHT_NIGHT",
                     observed_value=blue_max, threshold_value=cfg.blue_light_night_lux,
                     unit="lux@480nm", contribution=0.33),
        ]
        evidence += [
            Evidence(kind="BIOMARKER", ref_table="biomarker_result",
                     metric=f"CORTISOL_RATIO:{c}",
                     observed_value=self._latest_cortisol_ratio(snap, c),
                     threshold_value=cfg.cortisol_ratio_crit, unit="ratio",
                     contribution=0.33)
            for c in cortisol_affected
        ]

        return RuleOutcome(
            rule_id="SCENARIO_B_CIRCADIAN_VIRAL",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else Severity.OK,
            category="INTEGRATED",
            scenario=Scenario.B_CIRCADIAN_VIRAL,
            title="REACTIVACIÓN VIRAL POR DISRUPCIÓN CIRCADIANA",
            clinical_impact=(
                "Riesgo de herpes zóster oftálmico o diseminado: iniciar antiviral y "
                "resincronizar el eje HPA."
            ) if matched else "",
            risk_score=100.0 if matched else 0.0,
            evidence=evidence if matched else [],
            criteria=criteria,
            actions=self._actions_scenario_b(cortisol_affected, viral_zoster) if matched else [],
            dedup_suffix="B",
            window_start=snap.start,
            window_end=snap.end,
        )

    def _scenario_c(self, snap: Snapshot) -> RuleOutcome:
        """
        Escenario C — Proliferación fúngica aérea + crisis de hipersensibilidad.

        ``(HR > 70 % durante > 12 h AND hongos > 100 UFC/m3) AND
        (eosinófilos > 500/µL OR SpO2 < 94 % con sibilancias en >= 1)``
        """
        cfg = self.cfg
        sustained_h = hours_over(snap.env.get("HUMIDITY", []), cfg.humidity_sustained_pct)
        humidity_ok = sustained_h > cfg.humidity_sustained_hours

        fungi_max = max((float(s["fungi_cfu_m3"]) for s in snap.air), default=None)
        fungi_ok = fungi_max is not None and fungi_max > cfg.fungi_crit_cfu_m3

        eos_affected = [
            m.code for m in snap.crew
            if (eos := snap.bio_latest(m.code, Analyte.EOSINOPHIL_ABS.value)) is not None
            and eos > cfg.eosinophil_crit
        ]
        wheeze = [c for c, syms in snap.symptoms.items() if "WHEEZE" in syms]
        hypoxic = [
            m.code for m in snap.crew
            if (spo2 := snap.bio_latest(m.code, Analyte.SPO2.value)) is not None
            and spo2 < config.kpi("SPO2").crit_low
        ]
        clinical_ok = bool(eos_affected or (wheeze and hypoxic))

        criteria = [
            Criterion("HR > 70 % durante > 12 h", humidity_ok,
                      f"acumulado={sustained_h:.1f} h"),
            Criterion("Hongos en aire > 100 UFC/m3", fungi_ok, f"max={fungi_max}"),
            Criterion("Eosinófilos > 500/µL o SpO2 < 94 % con sibilancias", clinical_ok,
                      f"eos={eos_affected} sibilancias={wheeze} SpO2={hypoxic}"),
        ]
        matched = humidity_ok and fungi_ok and clinical_ok
        affected = sorted(set(eos_affected) | set(wheeze))

        evidence = [
            Evidence(kind="ENV", ref_table="sensor_reading", metric="HUMIDITY_HORAS_SOBRE_70",
                     observed_value=round(sustained_h, 1),
                     threshold_value=cfg.humidity_sustained_hours, unit="h",
                     contribution=0.34),
            Evidence(kind="MICRO", ref_table="air_microbio_sample", metric="AIR_FUNGI",
                     observed_value=fungi_max, threshold_value=cfg.fungi_crit_cfu_m3,
                     unit="UFC/m3", contribution=0.33),
        ]
        evidence += [
            Evidence(kind="BIOMARKER", ref_table="biomarker_result",
                     metric=f"EOSINOFILOS:{c}",
                     observed_value=snap.bio_latest(c, Analyte.EOSINOPHIL_ABS.value),
                     threshold_value=cfg.eosinophil_crit, unit="cel/uL", contribution=0.33)
            for c in affected
        ]

        return RuleOutcome(
            rule_id="SCENARIO_C_FUNGAL_ALLERGY",
            rule_version=self.RULE_VERSION,
            matched=matched,
            severity=Severity.CRITICAL if matched else Severity.OK,
            category="INTEGRATED",
            scenario=Scenario.C_FUNGAL_ALLERGY,
            title="PROLIFERACIÓN FÚNGICA EN AIRE DE CABINA",
            clinical_impact=(
                f"Riesgo alto de broncoespasmo en {len(affected)} tripulante(s); "
                "contener la humedad y descontaminar el punto de rocío."
            ) if matched else "",
            risk_score=100.0 if matched else 0.0,
            evidence=evidence if matched else [],
            criteria=criteria,
            actions=self._actions_scenario_c(affected) if matched else [],
            dedup_suffix="C",
            window_start=snap.start,
            window_end=snap.end,
        )

    # -- 4.7 planes de acción (protocolo de autonomía) --------------------

    @staticmethod
    def _actions_scenario_a(affected: list[str]) -> list[ActionDraft]:
        return [
            ActionDraft(
                sequence=1,
                phase="AISLAMIENTO",
                action_type="CLOSE_SOLENOID_VALVE",
                description=(
                    "Cerrar las válvulas solenoides del tanque A y conmutar el "
                    "suministro al Circuito B esterilizado."
                ),
                payload={"valves": ["VALVE-WRS-04A", "VALVE-WRS-04B"],
                         "switch_to": "CIRCUIT_B", "module": "HAB-01"},
                verification_type="PHOTO_MATCH",
                qr_code="QR-VALVE-WRS-04A",
            ),
            ActionDraft(
                sequence=2,
                phase="DESINFECCION",
                action_type="THERMAL_PASTEURIZATION",
                description=(
                    "Pasteurizar el circuito aislado a 85 °C durante 60 min y dosificar "
                    "biocida de plata iónica a 400 ppb."
                ),
                payload={"temp_c": 85, "duration_min": 60, "silver_ppb": 400},
                verification_type="TACTILE",
            ),
            ActionDraft(
                sequence=3,
                phase="CLINICO",
                action_type="MUCOSAL_PROPHYLAXIS_5D",
                description=(
                    "Protocolo profiláctico de 5 días: zinc 30 mg/día, vitamina D3 "
                    "4 000 UI/día y probiótico Lactobacillus rhamnosus GG."
                ),
                payload={"crew": affected, "days": 5, "zinc_mg": 30,
                         "vitamin_d3_ui": 4000, "probiotic": "LGG"},
                verification_type="QR_SCAN",
                qr_code="QR-ZN30-VITD4000-LGG",
                storage_location="Cajón 2, Celda B4",
            ),
            ActionDraft(
                sequence=4,
                phase="LOGISTICA",
                action_type="QUARANTINE_CIRCUIT_AND_DOWNLINK",
                description=(
                    "Bloquear el consumo del Circuito A hasta dos pruebas sucesivas "
                    "de ATP/qPCR negativas y empaquetar la telemetría para Tierra."
                ),
                payload={"circuit": "A", "clearance_tests": 2,
                         "criteria": ["ATP", "qPCR"]},
                verification_type="TACTILE",
            ),
        ]

    @staticmethod
    def _actions_scenario_b(affected: list[str], viral_zoster: list[str]) -> list[ActionDraft]:
        actions = [
            ActionDraft(
                sequence=1,
                phase="AISLAMIENTO",
                action_type="LIGHTING_SPECTRUM_OVERRIDE",
                description=(
                    "Anular el controlador de iluminación: bloquear el espectro < 550 nm "
                    "durante las 4 h previas al sueño y forzar modo ámbar."
                ),
                payload={"block_below_nm": 550, "hours_before_sleep": 4,
                         "mode": "AMBER"},
                verification_type="TACTILE",
            ),
            ActionDraft(
                sequence=2,
                phase="AISLAMIENTO",
                action_type="NOISE_CANCELLATION_AND_HPD",
                description=(
                    "Activar cancelación activa de ruido en los módulos de descanso y "
                    "ordenar protectores auditivos de 25 dB de atenuación."
                ),
                payload={"anc_modules": ["HAB-01"], "attenuation_db": 25},
                verification_type="PHOTO_MATCH",
                qr_code="QR-HPD-25DB",
                storage_location="Cajón 1, Celda A3",
            ),
            ActionDraft(
                sequence=3,
                phase="CLINICO",
                action_type="ANTIVIRAL_VALACYCLOVIR",
                description=(
                    "Extraer valaciclovir 1 000 mg cada 8 h durante 7 días del "
                    "dispensador médico seguro (indicado si VZV > 1e4 copias/mL)."
                ),
                payload={"drug": "VALACYCLOVIR", "dose_mg": 1000, "interval_h": 8,
                         "days": 7, "crew": viral_zoster or affected,
                         "indication": "VZV > 1e4 copias/mL"},
                verification_type="QR_SCAN",
                qr_code="QR-VALAC-1000",
                storage_location="Cajón 3, Celda A2",
            ),
            ActionDraft(
                sequence=4,
                phase="CLINICO",
                action_type="MELATONIN_CR",
                description=(
                    "Administrar melatonina de liberación controlada 3 mg una hora "
                    "antes del ciclo oscuro para resincronizar el eje HPA."
                ),
                payload={"drug": "MELATONIN_CR", "dose_mg": 3, "offset_h": -1,
                         "crew": affected},
                verification_type="QR_SCAN",
                qr_code="QR-MELAT-3CR",
                storage_location="Cajón 3, Celda C1",
            ),
            ActionDraft(
                sequence=5,
                phase="LOGISTICA",
                action_type="RECLASSIFY_TASKS_48H",
                description=(
                    "Reclasificar las tareas no críticas del tripulante afectado durante "
                    "48 h para reducir la carga alostática."
                ),
                payload={"crew": affected, "hours": 48},
                requires_human_confirmation=False,
                verification_type=None,
            ),
        ]
        return actions

    @staticmethod
    def _actions_scenario_c(affected: list[str]) -> list[ActionDraft]:
        return [
            ActionDraft(
                sequence=1,
                phase="AISLAMIENTO",
                action_type="MAX_DEHUMIDIFICATION_RECIRC",
                description=(
                    "Llevar los deshumidificadores de condensación al 100 % y activar "
                    "recirculación máxima a través de HEPA/ULPA."
                ),
                payload={"dehumidifier_pct": 100, "recirc": "MAX", "filters": "HEPA/ULPA"},
                requires_human_confirmation=False,
                verification_type=None,
            ),
            ActionDraft(
                sequence=2,
                phase="DESINFECCION",
                action_type="LOCALIZED_DECON_H2O2",
                description=(
                    "Generar el mapa térmico de humedad para localizar el punto de rocío "
                    "y limpiar la superficie con toallitas de H2O2 acelerado al 0.5 % "
                    "(respirador N95 y guantes de nitrilo)."
                ),
                payload={"agent": "H2O2_ACCELERATED_0.5", "ppe": ["N95", "NITRILE"]},
                verification_type="PHOTO_MATCH",
                qr_code="QR-H2O2-05",
                storage_location="Cajón 5, Celda D1",
            ),
            ActionDraft(
                sequence=3,
                phase="CLINICO",
                action_type="ANTIHISTAMINE_2ND_GEN",
                description=(
                    "Administrar cetirizina 10 mg vía oral y vigilar la respuesta en 60 min."
                ),
                payload={"drug": "CETIRIZINE", "dose_mg": 10, "route": "PO",
                         "crew": affected},
                verification_type="QR_SCAN",
                qr_code="QR-CETI-10",
                storage_location="Cajón 4, Celda A1",
            ),
            ActionDraft(
                sequence=4,
                phase="CLINICO",
                action_type="INHALED_BRONCHODILATOR_STEROID",
                description=(
                    "Si hay compromiso bronquial: salbutamol inhalado + budesonida "
                    "inhalada. Evitar corticoides sistémicos orales (agravarían la "
                    "inmunosupresión espacial)."
                ),
                payload={"drugs": ["SALBUTAMOL", "BUDESONIDE"], "route": "INHALED",
                         "avoid": ["ORAL_CORTICOSTEROIDS"], "crew": affected},
                verification_type="QR_SCAN",
                qr_code="QR-SALB-BUD",
                storage_location="Cajón 4, Celda B2",
            ),
        ]

    # -- 4.8 agrupación por causa raíz y supresión en cascada --------------

    @staticmethod
    def _subsystem_of(outcome: RuleOutcome) -> str | None:
        """Clasifica una señal atómica por subsistema ECLSS responsable."""
        rule = outcome.rule_id
        if rule.startswith("MICRO_WATER"):
            return "WATER"
        if rule.startswith("MICRO_") or rule.startswith("ENV_"):
            return "AIR"
        return None

    def _system_roots(self, outcomes: list[RuleOutcome]) -> list[RuleOutcome]:
        """
        Crea la alerta raíz del fallo primario cuando hay una cascada real.

        Es la implementación directa de la «supresión de cascada»: si un
        ventilador caído produce quince alarmas derivadas (CO2, temperatura,
        humedad, ruido, hongos...), el operador ve **una** alerta de sistema y
        las quince quedan archivadas debajo, consultables pero no gritando.

        El umbral (3 señales de aire, 2 de agua) evita declarar un fallo
        sistémico por una única alarma aislada.
        """
        rules = {
            "AIR": ("DEGRADACIÓN DEL SISTEMA DE AIRE", 3,
                    "Fallo primario de revitalización de aire: HEPA/carbón saturados "
                    "y lecho de CO2 agotado."),
            "WATER": ("DEGRADACIÓN DEL SISTEMA DE AGUA", 2,
                      "Biopelícula en el WRS: retirar el circuito afectado del consumo."),
        }
        roots: list[RuleOutcome] = []
        for subsystem, (title, min_count, impact) in rules.items():
            atomics = [
                o for o in outcomes
                if o.matched and o.category in {"ENV", "MICRO"}
                and self._subsystem_of(o) == subsystem
            ]
            if len(atomics) < min_count:
                continue
            severity = worst(*[o.severity for o in atomics])
            top = sorted(atomics, key=lambda o: (-o.risk_score, o.rule_id))[:6]
            roots.append(
                RuleOutcome(
                    rule_id=f"SYSTEM_{subsystem}_DEGRADED",
                    rule_version=self.RULE_VERSION,
                    matched=True,
                    severity=Severity.CRITICAL if severity is Severity.CRITICAL else Severity.WARNING,
                    category="SYSTEM",
                    title=title,
                    clinical_impact=(
                        f"{len(atomics)} alarmas derivadas agrupadas. {impact}"
                    ),
                    risk_score=95.0,
                    evidence=[ev for o in top for ev in o.evidence[:1]],
                    subsystem=subsystem,
                    dedup_suffix=subsystem,
                    notes=f"agrupa={len(atomics)} señales",
                    window_start=atomics[0].window_start,
                    window_end=atomics[0].window_end,
                )
            )
        return roots

    def apply_cascade_suppression(self, outcomes: list[RuleOutcome]) -> list[RuleOutcome]:
        """
        Archiva las alarmas derivadas bajo la alerta raíz de su subsistema.

        Decisión deliberada: **sólo se ocultan alarmas ambientales y
        microbiológicas**, y sólo cuando existe una alerta de sistema que las
        explique. Las alertas clínicas (biomarcadores) y los escenarios de
        autonomía nunca se suprimen: ocultar un dato del paciente es un riesgo
        médico, no una mejora de usabilidad.
        """
        roots = {o.subsystem: o for o in outcomes if o.category == "SYSTEM" and o.matched}
        if not roots:
            return outcomes

        for outcome in outcomes:
            if outcome.category not in {"ENV", "MICRO"} or not outcome.matched:
                continue
            root = roots.get(self._subsystem_of(outcome) or "")
            if root is not None:
                outcome.suppressed_by = root.rule_id
        return outcomes

    # -- 4.9 orquestación -------------------------------------------------

    def run(self, end: datetime | None = None, *, persist: bool = True) -> EngineRun:
        """
        Ejecuta el ciclo completo: snapshot → reglas → supresión → persistencia.

        Parameters
        ----------
        end:
            Extremo de la ventana. Por omisión, la última telemetría disponible.
        persist:
            Si es ``False`` el motor no escribe nada (modo simulación / tests).
        """
        started = utcnow()
        end = end or self._latest_telemetry()
        start = end - timedelta(hours=self.cfg.window_hours)
        snap = self.build_snapshot(start, end)

        outcomes: list[RuleOutcome] = []

        # (1) Reglas atómicas ambientales, por módulo y canal.
        for module_code, channels in snap.env_by_module.items():
            for kpi_key, series in channels.items():
                # Canales derivados (NOISE_NIGHT) no tienen umbral propio: se
                # consumen en las rutas y escenarios, no como regla atómica.
                if not series or kpi_key not in config.KPI:
                    continue
                outcomes.append(self._rule_env_kpi(snap, module_code, kpi_key, series))

        # (2) Microbiología.
        outcomes.append(self._rule_air_fungi(snap))
        outcomes.append(self._rule_air_bacteria(snap))
        outcomes.extend(self._rule_surfaces(snap))
        outcomes.extend(self._rule_water(snap))

        # (3) Biomarcadores.
        outcomes.append(self._rule_siga(snap))
        outcomes.append(self._rule_cortisol(snap))
        outcomes.append(self._rule_viral(snap))
        outcomes.append(self._rule_cytokines(snap))
        outcomes.append(self._rule_differential(snap))

        # (4) Rutas fisiopatológicas y (5) escenarios de autonomía.
        outcomes.append(self._route_r1(snap))
        outcomes.append(self._route_r2(snap))
        outcomes.append(self._route_r3(snap))
        outcomes.append(self._scenario_a(snap))
        outcomes.append(self._scenario_b(snap))
        outcomes.append(self._scenario_c(snap))

        # (6) Agrupación por causa raíz: se añade la alerta de sistema y se
        # marcan como suprimidas sus derivadas.
        outcomes.extend(self._system_roots(outcomes))
        outcomes = self.apply_cascade_suppression(outcomes)

        run = EngineRun(
            started_at=started,
            finished_at=utcnow(),
            window_start=start,
            window_end=end,
            outcomes=outcomes,
        )

        if persist:
            self._persist(run)

        run.finished_at = utcnow()
        return run

    def _latest_telemetry(self) -> datetime:
        bounds = self.repo.time_bounds()
        if not bounds:
            raise LookupError("No hay telemetría: ejecuta el generador sintético primero.")
        return bounds[1]

    def _dedup_key(self, outcome: RuleOutcome, window_end: datetime) -> str:
        """
        Clave de idempotencia estable dentro de un cubo temporal.

        Evita que el motor re-emita la misma alerta en cada corrida de 30 s
        mientras el episodio sigue activo (el operador ve una alerta, no cien).
        """
        bucket_h = self.cfg.dedup_bucket_hours
        floored = window_end.replace(minute=0, second=0, microsecond=0)
        floored -= timedelta(hours=floored.hour % bucket_h)
        return f"{outcome.rule_id}|{outcome.dedup_suffix}|{to_iso(floored)}"

    def _persist(self, run: EngineRun) -> None:
        """Guarda alertas raíz primero y luego las derivadas suprimidas."""
        ordered = sorted(
            run.actionable, key=lambda o: (o.suppressed_by is not None, -o.risk_score)
        )
        suppressed_count: dict[str, int] = {}
        for outcome in run.actionable:
            if outcome.suppressed_by:
                suppressed_count[outcome.suppressed_by] = (
                    suppressed_count.get(outcome.suppressed_by, 0) + 1
                )

        for outcome in ordered:
            alert_row = {
                "mission_id": self._mission_id,
                "created_at": to_iso(run.window_end),
                "rule_id": outcome.rule_id,
                "rule_version": outcome.rule_version,
                "route": outcome.route.value if outcome.route else None,
                "scenario": outcome.scenario.value.split("_")[0] if outcome.scenario else None,
                "severity": outcome.severity.value,
                "category": outcome.category,
                "title": outcome.title,
                "clinical_impact": outcome.clinical_impact,
                "status": (
                    AlertStatus.SUPPRESSED.value if outcome.suppressed_by else AlertStatus.OPEN.value
                ),
                "dedup_key": self._dedup_key(outcome, run.window_end),
                "root_cause_alert_id": run.created_alert_ids.get(outcome.suppressed_by or ""),
                "suppressed_count": suppressed_count.get(outcome.rule_id, 0),
                "risk_score": outcome.risk_score,
                "threshold_version": THRESHOLD_VERSION,
                "window_start": to_iso(outcome.window_start or run.window_start),
                "window_end": to_iso(outcome.window_end or run.window_end),
            }
            alert_id = self.repo.save_alert(
                alert_row,
                [ev.as_row() for ev in outcome.evidence],
                outcome.actions_for_payload,
            )
            run.created_alert_ids[outcome.rule_id] = alert_id

        # Auditoría: se registra también lo que NO disparó (negativos).
        for outcome in run.outcomes:
            self.repo.log_rule_execution(
                {
                    "run_at": to_iso(run.finished_at),
                    "rule_id": outcome.rule_id,
                    "rule_version": outcome.rule_version,
                    "matched": int(outcome.matched),
                    "severity": outcome.severity.value,
                    "window_start": to_iso(run.window_start),
                    "window_end": to_iso(run.window_end),
                    "latency_ms": round(run.latency_ms, 3),
                    "notes": "; ".join(str(c) for c in outcome.criteria) or outcome.notes,
                }
            )

        # Si hay escenario crítico, se empaqueta la telemetría para Tierra.
        for outcome in run.actionable:
            if outcome.scenario and outcome.severity is Severity.CRITICAL:
                self.repo.queue_downlink(
                    "ALERT_BUNDLE",
                    {
                        "scenario": outcome.scenario.value,
                        "rule_id": outcome.rule_id,
                        "title": outcome.title,
                        "evidence": [ev.as_row() for ev in outcome.evidence],
                        "actions": [a.as_row() for a in outcome.actions],
                    },
                    window_from=run.window_start,
                    window_to=run.window_end,
                )


__all__ = [
    "ActionDraft",
    "CorrelationEngine",
    "Criterion",
    "EngineRun",
    "Evidence",
    "RuleOutcome",
    "SeriesStats",
    "Snapshot",
    "consecutive_days",
    "hours_over",
    "leq",
    "nights_over",
    "pct_change",
    "stats",
    "viral_copies",
]
