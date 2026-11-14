"""
eclss.synthetic — Generador de telemetría sintética para el MVP.

Objetivo
--------
Producir una historia de **96 h de misión**: 24 h nominales (el requisito) más
72 h de avería progresiva, desde un fallo primario hasta sus consecuencias
clínicas, de modo que el motor de reglas tenga algo que correlacionar:

    h 0–24    Operación nominal (línea base y línea base individual de la tripulación).
    h 24–40   Saturación progresiva de filtros HEPA/carbón: suben humedad y
              TVOC; el soplador del ventilador empieza a rozar (ruido).
    h 34–48   Degradación del lecho de CO2 y deriva del driver LED: `ppCO2`
              sube, la temperatura se va, y por la noche entra luz azul.
    h 44–60   Biopelícula en el WRS: TOC y bacterias del Circuito A escalan.
    h 60–96   Respuesta de la tripulación: cae la sIgA, sube la IL-6, se aplana
              el cortisol y se reactivan herpesvirus latentes.

¿Por qué 96 h y no 24 h?
------------------------
La especificación pide «24 h de telemetría normal con una anomalía
progresiva». Esas 24 h están ahí, íntegras, al principio. Se añaden 72 h de
avería por una razón dura: el Escenario B exige **3 noches consecutivas** de
ruido > 62 dBA y un cortisol aplanado **3 días**. Ninguna ventana de 24 h puede
satisfacer esa conjunción, de modo que con sólo 24 h el escenario B no podría
dispararse nunca —ni en la simulación ni en vuelo real.

El motor mantiene una ventana de análisis de 24 h para el entorno y *lookbacks*
propios (72 h) para los criterios que por definición son multi-día.

Con ``hours=24`` se obtiene exactamente el primer día nominal: ninguna alerta
dispara, y eso es el resultado correcto (sirve como control de falsos positivos,
cubierto por los tests). Para ver los tres escenarios hacen falta las 96 h,
porque el Escenario B exige 3 noches consecutivas y el A y el C requieren que la
avería haya progresado.

Fidelidad temporal
------------------
Las frecuencias declaradas (1 Hz para presión y `ppO2`, 0.5 Hz para `ppCO2`,
0.1 Hz para temperatura y humedad) suponen ~7,7 millones de filas en 72 h para
30 sensores. Para un MVP local se comprime a una cadencia configurable
(``interval_minutes``, 2 min por omisión) y se documenta el factor:
``1 Hz → 1/30 Hz``. La densidad no cambia ninguna regla, sólo la resolución.

Uso
---
    from eclss.db import connect, Repository
    from eclss.synthetic import SyntheticMission

    repo = Repository(connect("out/eclss_mvp.sqlite3"))
    repo.seed()
    SyntheticMission().generate(repo)
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator

from . import config
from .config import Analyte, SampleType, SensorKind
from .db import Repository

#: Origen de la simulación: fecha fija para que la demo sea reproducible.
SIM_START = datetime(2025, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

#: Fin de la rampa de degradación (en horas desde el inicio).
#: Las primeras 24 h son nominales; la avería progresa hasta estabilizarse.
ANOMALY_START_H = 24.0
ANOMALY_FULL_H = 60.0

#: Perfil por módulo: (Δtemperatura °C, Δruido dBA, Δhumedad %HR, ΔCO2 kPa·factor).
MODULE_PROFILE: dict[str, tuple[float, float, float, float]] = {
    "HAB-01": (0.0, 0.0, 0.0, 1.00),
    "LAB-01": (-0.7, 3.0, -2.5, 1.06),
    "NODE-01": (0.5, 1.5, 1.0, 0.97),
}

#: Factor de vulnerabilidad por tripulante para el eje inflamatorio Th1/Th2.
#: Sin él, los cinco tripulantes presentarían curvas de citoquinas idénticas —
#: un artefacto evidente para cualquier clínico que leyese el panel. MS-03, como
#: médico de a bordo con menor carga alostática, mantiene el perfil Th1.
STRESS_FACTOR: dict[str, float] = {
    "CDR-01": 1.00,
    "PLT-02": 0.92,
    "MS-03": 0.45,
    "MS-04": 1.05,
    "MS-05": 0.80,
}

#: Valores finales (fin de rampa) por tripulante.
#: Los tripulantes con mayor carga alostática son CDR-01 y MS-04 (sIgA muy
#: deprimida), PLT-02 (reactivación de EBV) y MS-05 (fenotipo alérgico).
CREW_ENDPOINTS: dict[str, dict[str, float]] = {
    "CDR-01": {"ratio": 1.70, "siga": 88.0, "il6": 6.8, "ebv": 4.0e3, "cmv": 90.0,
               "vzv": 1.2e4, "eos": 240.0, "neut_pct": 68.0, "lymph": 1150.0,
               "spo2": 96.5, "ige": 70.0},
    "PLT-02": {"ratio": 1.90, "siga": 120.0, "il6": 4.1, "ebv": 3.5e4, "cmv": 60.0,
               "vzv": 80.0, "eos": 210.0, "neut_pct": 66.0, "lymph": 1300.0,
               "spo2": 96.0, "ige": 60.0},
    "MS-03": {"ratio": 3.40, "siga": 160.0, "il6": 2.2, "ebv": 60.0, "cmv": 45.0,
              "vzv": 40.0, "eos": 150.0, "neut_pct": 58.0, "lymph": 2000.0,
              "spo2": 97.5, "ige": 45.0},
    "MS-04": {"ratio": 1.80, "siga": 70.0, "il6": 7.4, "ebv": 120.0, "cmv": 110.0,
              "vzv": 8.0e3, "eos": 190.0, "neut_pct": 78.0, "lymph": 900.0,
              "spo2": 95.8, "ige": 80.0},
    "MS-05": {"ratio": 2.30, "siga": 95.0, "il6": 5.6, "ebv": 90.0, "cmv": 55.0,
              "vzv": 55.0, "eos": 640.0, "neut_pct": 61.0, "lymph": 1600.0,
              "spo2": 93.5, "ige": 240.0},
}


# --------------------------------------------------------------------------- #
# Utilidades matemáticas
# --------------------------------------------------------------------------- #


def progression(hour: float) -> float:
    """Rampa 0→1 de la degradación (curva suave, no lineal al principio)."""
    if hour <= ANOMALY_START_H:
        return 0.0
    if hour >= ANOMALY_FULL_H:
        return 1.0
    x = (hour - ANOMALY_START_H) / (ANOMALY_FULL_H - ANOMALY_START_H)
    return x * x * (3 - 2 * x)  # smoothstep: arranque lento, meseta final


def daily_wave(hour: float, amplitude: float = 1.0, peak_hour: float = 15.0) -> float:
    """Oscilación circadiana de ±``amplitude`` con máximo a ``peak_hour``."""
    return amplitude * math.sin(2 * math.pi * (hour - peak_hour) / 24.0)


def is_night_hour(hour: float) -> bool:
    """
    Fase de sueño del hábitat: 22:00–06:00.

    Debe coincidir exactamente con ``config.is_night`` (el motor filtra con
    esa función); si divergen, el generador produce datos diurnos que el motor
    clasifica como nocturnos y las alertas de luz se disparan en falso.
    """
    h = hour % 24.0
    return h >= 22.0 or h < 6.0


def light_level(hour: float) -> float:
    """
    Fracción de iluminación diurna programada (0 = oscuro, 1 = pleno día).

    Incluye rampas de amanecer (06:00–07:00) y anochecer (21:00–22:00) para que
    la serie no tenga escalones de 500 lux entre dos muestras consecutivas. La
    ventana 22:00–06:00 queda estrictamente a cero para que coincida con
    ``config.is_night``: si la rampa invadiera la fase de sueño, el motor
    clasificaría luz diurna como nocturna y dispararía una alerta falsa.
    """
    h = hour % 24.0
    if 7.0 <= h < 21.0:
        return 1.0
    if 6.0 <= h < 7.0:
        return h - 6.0
    if 21.0 <= h < 22.0:
        return 22.0 - h
    return 0.0


# --------------------------------------------------------------------------- #
# Modelo de degradación
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DegradationModel:
    """
    Modelo determinista de la avería: cada canal es función de ``hour``.

    Se separa del bucle de inserción para que sea testeable y para que el
    reporte pueda explicar «qué valor esperaba el modelo» frente a «qué leyó
    el sensor».
    """

    seed: int = 20250301
    noise_sigma: dict[str, float] = field(
        default_factory=lambda: {
            "PRESSURE_TOTAL": 0.12,
            "PPO2": 0.10,
            "PPCO2": 0.006,
            "TEMPERATURE": 0.10,
            "HUMIDITY": 0.55,
            "NOISE_LEQ": 1.1,
            "NOISE_PEAK": 2.2,
            "LIGHT_TOTAL": 1.5,
            "LIGHT_BLUE_480": 0.9,
            "VOC_TVOC": 6.0,
        }
    )

    def _rng(self) -> random.Random:
        # Ruido reproducible por hora: misma perturbación si se regenera.
        return random.Random(self.seed)

    def value(self, kind: SensorKind, hour: float, module: str = "HAB-01",
              rng: random.Random | None = None) -> float:
        """Valor instantáneo del canal ``kind`` en la hora ``hour``."""
        p = progression(hour)
        dt, dnoise, dhum, dco2 = MODULE_PROFILE.get(module, (0.0, 0.0, 0.0, 1.0))
        night = is_night_hour(hour)
        light = light_level(hour)

        if kind is SensorKind.PRESSURE_TOTAL:
            base = 101.0 - 0.10 * p
        elif kind is SensorKind.PPO2:
            base = 21.05 + 0.15 * math.sin(hour / 7.0) - 0.35 * p
        elif kind is SensorKind.PPCO2:
            # Lecho de CO2 degradado: la concentración crece de forma convexa.
            base = (0.225 + 0.45 * (p ** 1.7)) * dco2
        elif kind is SensorKind.TEMPERATURE:
            base = 22.0 + dt + daily_wave(hour, 0.35, peak_hour=16) + 4.6 * p
        elif kind is SensorKind.HUMIDITY:
            base = 51.0 + dhum + daily_wave(hour, 1.8, peak_hour=4) + 27.0 * (p ** 1.25)
        elif kind is SensorKind.NOISE_LEQ:
            base = (42.5 if night else 47.5) + dnoise + 19.5 * p
            # El rodamiento degradado añade una componente tonal estable.
            base += 1.8 * p * math.sin(2 * math.pi * hour / 3.7)
        elif kind is SensorKind.NOISE_PEAK:
            base = (45.0 if night else 51.0) + dnoise + 24.0 * p
        elif kind is SensorKind.LIGHT_TOTAL:
            if light > 0:
                base = 2.5 + (520.0 - 2.5) * light + daily_wave(hour, 25.0)
            else:
                # Deriva del driver LED: el módulo ya no baja a oscuras.
                base = 2.5 + 27.0 * p
        elif kind is SensorKind.LIGHT_BLUE_480:
            if light > 0:
                base = 1.5 + (172.0 - 1.5) * light + daily_wave(hour, 9.0)
            else:
                # Fuga de azul en fase de sueño: el disparador de la Ruta 1.
                base = 1.5 + 64.0 * (p ** 1.4)
        elif kind is SensorKind.VOC_TVOC:
            base = 180.0 + 700.0 * (p ** 1.5)
        else:  # pragma: no cover - canal no previsto
            base = 0.0

        sigma = self.noise_sigma.get(kind.name, 0.0)
        jitter = rng.gauss(0.0, sigma) if (rng and sigma) else 0.0
        return round(base + jitter, 4)

    # -- series microbiológicas y de laboratorio --------------------------

    def fungi_cfu(self, hour: float) -> float:
        return 28.0 + 118.0 * (progression(hour) ** 1.8)

    def bacteria_cfu(self, hour: float) -> float:
        return 310.0 + 480.0 * (progression(hour) ** 1.5)

    def mvocs_ppb(self, hour: float) -> float:
        return 60.0 + 240.0 * progression(hour)

    def surface_cfu(self, hour: float) -> float:
        return 55.0 + 300.0 * (progression(hour) ** 1.6)

    def toc_mg_l(self, hour: float, circuit: str = "A") -> float:
        if circuit == "B":
            return 1.6 + 0.4 * progression(hour)
        return 2.35 + 4.15 * (progression(hour) ** 1.8)

    def water_bacteria(self, hour: float, circuit: str = "A") -> float:
        if circuit == "B":
            return 12.0 + 10.0 * progression(hour)
        return 32.0 + 122.0 * (progression(hour) ** 1.9)

    def fungal_genera(self, hour: float) -> list[str]:
        p = progression(hour)
        if p < 0.35:
            return ["CLADOSPORIUM"]
        if p < 0.6:
            return ["CLADOSPORIUM", "PENICILLIUM"]
        return ["ASPERGILLUS", "PENICILLIUM", "CLADOSPORIUM"]

    # -- curvas de la tripulación ----------------------------------------

    def crew_value(self, crew: str, analyte: str, hour: float) -> float:
        """
        Valor del analito ``analyte`` para ``crew`` en la hora ``hour``.

        Las curvas se interpolan con exponentes distintos por analito porque la
        fisiología no es lineal: la sIgA cae de forma temprana y sostenida,
        mientras que la carga viral sólo despega al final (reactivación).
        """
        p = progression(hour)
        target = CREW_ENDPOINTS[crew]

        if analyte == Analyte.SIGA.value:
            base = self._siga_baseline(crew)
            return round(base * (1.0 - (1.0 - target["siga"] / base) * (p ** 0.7)), 2)

        if analyte == Analyte.CORTISOL.value:
            return float("nan")  # se resuelve en `cortisol_pair`
        if analyte == Analyte.IL6.value:
            return round(1.8 + (target["il6"] - 1.8) * (p ** 2.2), 2)
        if analyte in (Analyte.EBV_DNA.value, Analyte.VZV_DNA.value, Analyte.CMV_DNA.value):
            key = {Analyte.EBV_DNA.value: "ebv", Analyte.VZV_DNA.value: "vzv",
                   Analyte.CMV_DNA.value: "cmv"}[analyte]
            endpoint = target[key]
            return round(45.0 + (endpoint - 45.0) * (p ** 6.0), 1)
        if analyte == Analyte.TNF_A.value:
            ps = p * STRESS_FACTOR.get(crew, 1.0)
            return round(3.2 + 6.5 * (ps ** 2.0), 2)
        if analyte == Analyte.IL10.value:
            ps = p * STRESS_FACTOR.get(crew, 1.0)
            return round(2.4 + 3.2 * (ps ** 1.5), 2)
        if analyte == Analyte.IFN_G.value:
            ps = p * STRESS_FACTOR.get(crew, 1.0)
            return round(9.5 - 6.8 * (ps ** 1.4), 2)
        if analyte == Analyte.IL4.value:
            ps = p * STRESS_FACTOR.get(crew, 1.0)
            return round(1.9 + 2.4 * (ps ** 1.6), 2)
        if analyte == Analyte.IL5.value:
            ps = p * STRESS_FACTOR.get(crew, 1.0)
            return round(1.4 + 2.2 * (ps ** 1.6), 2)
        if analyte == Analyte.IGE.value:
            base = 45.0 if not _is_allergic(crew) else 90.0
            return round(base + (target["ige"] - base) * (p ** 1.7), 1)
        if analyte == Analyte.EOSINOPHIL_ABS.value:
            return round(160.0 + (target["eos"] - 160.0) * (p ** 2.4), 0)
        if analyte == Analyte.NEUTROPHIL_PCT.value:
            return round(58.0 + (target["neut_pct"] - 58.0) * (p ** 1.5), 1)
        if analyte == Analyte.LYMPHOCYTE_ABS.value:
            return round(2100.0 + (target["lymph"] - 2100.0) * (p ** 1.5), 0)
        if analyte == Analyte.WBC.value:
            return round(6400.0 + 900.0 * p, 0)
        if analyte == Analyte.SPO2.value:
            return round(98.0 - (98.0 - target["spo2"]) * (p ** 2.2), 1)
        raise ValueError(f"Analito sin curva: {analyte}")

    def cortisol_pair(self, crew: str, hour: float) -> tuple[float, float]:
        """
        Devuelve ``(AM, NOCHE)`` en nmol/L para el día de ``hour``.

        Se modela el *ratio* y no cada punto por separado: el aplanamiento de la
        curva es una contracción simultánea del pico matutino y una elevación
        del nadir nocturno, ambas proporcionales a la carga alostática.
        """
        p = progression(hour)
        target = CREW_ENDPOINTS[crew]
        am_base = self._cortisol_am_baseline(crew)
        night_base = am_base / 9.0
        ratio = 9.0 + (target["ratio"] - 9.0) * p
        am = am_base * (1.0 - 0.42 * p)
        night = max(0.6, am / ratio if ratio else am)
        return round(am, 2), round(max(night, night_base), 2)

    # -- líneas base de referencia ---------------------------------------

    @staticmethod
    def _siga_baseline(crew: str) -> float:
        for code, _role, _sex, _by, siga, _c in config.CREW_SPECS:
            if code == crew:
                return siga
        return 140.0

    @staticmethod
    def _cortisol_am_baseline(crew: str) -> float:
        for code, _role, _sex, _by, _siga, cortisol in config.CREW_SPECS:
            if code == crew:
                return cortisol
        return 15.0


def _is_allergic(crew: str) -> bool:
    return crew == "MS-05"


# --------------------------------------------------------------------------- #
# Generador
# --------------------------------------------------------------------------- #


@dataclass
class GenerationStats:
    """Contadores de lo generado (se muestran al final y en los tests)."""

    readings: int = 0
    air_samples: int = 0
    surface_swabs: int = 0
    water_samples: int = 0
    lab_samples: int = 0
    biomarker_results: int = 0
    symptoms: int = 0
    hours: float = 0.0
    interval_minutes: int = 2

    def as_dict(self) -> dict[str, float]:
        return {
            "telemetria_filas": self.readings,
            "muestras_aire": self.air_samples,
            "hisopados": self.surface_swabs,
            "muestras_agua": self.water_samples,
            "muestras_lab": self.lab_samples,
            "resultados_biomarcador": self.biomarker_results,
            "sintomas": self.symptoms,
            "horas_simuladas": round(self.hours, 1),
            "cadencia_min": self.interval_minutes,
        }


class SyntheticMission:
    """
    Escribe una misión sintética completa en el repositorio.

    Parameters
    ----------
    hours:
        Duración simulada. 96 por omisión: 24 h nominales + 72 h de avería
        (ver la nota del módulo sobre el Escenario B). ``hours=24`` es válido
        y útil para pruebas rápidas.
    interval_minutes:
        Cadencia de telemetría. 2 min comprime las frecuencias declaradas.
    start:
        Instante inicial (UTC).
    seed:
        Semilla del generador de ruido; misma semilla → misma telemetría.
    """

    def __init__(
        self,
        *,
        hours: float = 96.0,
        interval_minutes: int = 2,
        start: datetime = SIM_START,
        seed: int = 20250301,
    ) -> None:
        if hours <= 0:
            raise ValueError("hours debe ser > 0")
        if interval_minutes <= 0:
            raise ValueError("interval_minutes debe ser > 0")
        self.hours = float(hours)
        self.interval_minutes = int(interval_minutes)
        self.start = start
        self.model = DegradationModel(seed=seed)
        self.rng = random.Random(seed)
        self.stats = GenerationStats(hours=self.hours, interval_minutes=self.interval_minutes)

    # -- API pública ------------------------------------------------------

    def generate(self, repo: Repository) -> GenerationStats:
        """Genera y persiste la misión completa. Idempotente por restricción UNIQUE."""
        module_codes = [m for m, _n, _v, _k in config.MODULE_SPECS]
        sensor_ids = {
            (module, kind.name): repo.sensor_id(module, kind)
            for module in module_codes
            for kind, _u, _hz, _l in config.SENSOR_SPECS
        }

        self._generate_telemetry(repo, sensor_ids)
        self._generate_air_samples(repo)
        self._generate_surface_swabs(repo)
        self._generate_water_samples(repo)
        self._generate_lab_panels(repo)
        self._generate_symptoms(repo)
        return self.stats

    # -- telemetría -------------------------------------------------------

    def _timestamps(self) -> Iterator[datetime]:
        step = timedelta(minutes=self.interval_minutes)
        end = self.start + timedelta(hours=self.hours)
        ts = self.start
        while ts < end:
            yield ts
            ts += step

    def _generate_telemetry(self, repo: Repository,
                            sensor_ids: dict[tuple[str, str], int]) -> None:
        buffer: list[tuple[int, datetime, float, str]] = []
        for ts in self._timestamps():
            hour = (ts - self.start).total_seconds() / 3600.0
            # RNG derivado de la marca de tiempo: reproducible e independiente
            # del orden de recorrido.
            rng = random.Random(f"{self.model.seed}:{ts.isoformat()}")
            for module, _name, _vol, _kind in config.MODULE_SPECS:
                for kind, _unit, _hz, _label in config.SENSOR_SPECS:
                    value = self.model.value(kind, hour, module, rng)
                    buffer.append((sensor_ids[(module, kind.name)], ts, value, "OK"))

        if buffer:
            self.stats.readings = repo.add_readings(buffer)

    # -- microbiología ----------------------------------------------------

    def _sample_times(self, every_h: float) -> list[datetime]:
        times = []
        t = self.start + timedelta(hours=every_h)
        limit = self.start + timedelta(hours=self.hours)
        while t <= limit:
            times.append(t)
            t += timedelta(hours=every_h)
        return times

    def _generate_air_samples(self, repo: Repository) -> None:
        rng = random.Random(self.model.seed + 7)
        for ts in self._sample_times(every_h=12):
            hour = (ts - self.start).total_seconds() / 3600.0
            p = progression(hour)
            for module in ("HAB-01", "LAB-01"):
                scale = 1.0 if module == "HAB-01" else 0.72
                repo.add_air_sample(
                    module,
                    ts,
                    bacteria_cfu_m3=round((self.model.bacteria_cfu(hour) + rng.gauss(0, 18)) * scale, 1),
                    fungi_cfu_m3=round((self.model.fungi_cfu(hour) + rng.gauss(0, 5)) * scale, 1),
                    dominant_fungi=self.model.fungal_genera(hour),
                    mvocs_ppb=round(self.model.mvocs_ppb(hour) + rng.gauss(0, 8), 1),
                    method="IMPACTOR_6STAGE",
                    sampled_by="MS-04" if p < 0.5 else "MS-03",
                )
                self.stats.air_samples += 1

    def _generate_surface_swabs(self, repo: Repository) -> None:
        rng = random.Random(self.model.seed + 11)
        surfaces = [
            ("HAB-01", "GALLEY_COUNTER"),
            ("HAB-01", "HYGIENE_PANEL"),
            ("NODE-01", "ECLSS_CONTROL_PANEL"),
        ]
        for ts in self._sample_times(every_h=24):
            hour = (ts - self.start).total_seconds() / 3600.0
            p = progression(hour)
            for module, label in surfaces:
                cfu = self.model.surface_cfu(hour) + rng.gauss(0, 12)
                final = p > 0.85 and label == "GALLEY_COUNTER"
                repo.add_surface_swab(
                    module,
                    ts,
                    label,
                    total_cfu_per_100cm2=round(max(5.0, cfu), 1),
                    staphylococcus_aureus=bool(final),
                    mrsa=False,
                    pseudomonas_aeruginosa=bool(p > 0.9 and label == "HYGIENE_PANEL"),
                    minion_pathogens=(
                        ["STAPHYLOCOCCUS_AUREUS"] if final else []
                    ),
                    atp_rlu=round(40.0 + 900.0 * p + rng.gauss(0, 25), 1),
                    sampled_by="MS-03",
                )
                self.stats.surface_swabs += 1

    def _generate_water_samples(self, repo: Repository) -> None:
        rng = random.Random(self.model.seed + 13)
        for ts in self._sample_times(every_h=12):
            hour = (ts - self.start).total_seconds() / 3600.0
            p = progression(hour)
            for circuit in ("A", "B"):
                toc = self.model.toc_mg_l(hour, circuit) + rng.gauss(0, 0.05)
                bact = self.model.water_bacteria(hour, circuit) + rng.gauss(0, 3.5)
                repo.add_water_sample(
                    "HAB-01",
                    circuit,
                    ts,
                    toc_mg_l=round(max(0.3, toc), 2),
                    total_bacteria_cfu_ml=round(max(1.0, bact), 1),
                    coliforms_present=bool(circuit == "A" and p > 0.92),
                    silver_ppb=round(392.0 + rng.gauss(0, 12), 1) if circuit == "A" else None,
                    turbidity_ntu=round(0.4 + 1.8 * p, 2),
                    atp_rlu=round(60.0 + 520.0 * p, 1),
                )
                self.stats.water_samples += 1

    # -- paneles de laboratorio ------------------------------------------

    def _generate_lab_panels(self, repo: Repository) -> None:
        """Paneles diarios: cortisol (08:00/23:00), saliva, sangre capilar, qPCR."""
        rng = random.Random(self.model.seed + 17)
        day = self.start
        end = self.start + timedelta(hours=self.hours)
        panels: list[tuple[datetime, str, SampleType, list[tuple[Analyte, str, float]]]] = []

        while day < end:
            for crew, *_rest in config.CREW_SPECS:
                am_ts = day.replace(hour=8)
                pm_ts = day.replace(hour=23)
                if not (self.start <= am_ts < end):
                    am_ts = None  # type: ignore[assignment]
                if not (self.start <= pm_ts < end):
                    pm_ts = None  # type: ignore[assignment]

                if am_ts:
                    hour = (am_ts - self.start).total_seconds() / 3600.0
                    am, night = self.model.cortisol_pair(crew, hour)
                    panels.append((am_ts, crew, SampleType.SALIVA,
                                   [(Analyte.CORTISOL, am, "nmol/L")]))
                if pm_ts:
                    hour = (pm_ts - self.start).total_seconds() / 3600.0
                    am, night = self.model.cortisol_pair(crew, hour)
                    panels.append((pm_ts, crew, SampleType.SALIVA,
                                   [(Analyte.CORTISOL, night, "nmol/L")]))

                if am_ts:
                    hour = (am_ts - self.start).total_seconds() / 3600.0
                    panels.append((
                        am_ts.replace(hour=7), crew, SampleType.SALIVA,
                        [
                            (Analyte.SIGA, self.model.crew_value(crew, "SIGA", hour), "ug/mL"),
                            (Analyte.EBV_DNA, self.model.crew_value(crew, "EBV_DNA", hour), "copias/mL"),
                            (Analyte.CMV_DNA, self.model.crew_value(crew, "CMV_DNA", hour), "copias/mL"),
                            (Analyte.VZV_DNA, self.model.crew_value(crew, "VZV_DNA", hour), "copias/mL"),
                        ],
                    ))
                    panels.append((
                        am_ts.replace(hour=7, minute=15), crew, SampleType.CAPILLARY_BLOOD,
                        [
                            (Analyte.IL6, self.model.crew_value(crew, "IL6", hour), "pg/mL"),
                            (Analyte.TNF_A, self.model.crew_value(crew, "TNF_A", hour), "pg/mL"),
                            (Analyte.IL10, self.model.crew_value(crew, "IL10", hour), "pg/mL"),
                            (Analyte.IFN_G, self.model.crew_value(crew, "IFN_G", hour), "pg/mL"),
                            (Analyte.IL4, self.model.crew_value(crew, "IL4", hour), "pg/mL"),
                            (Analyte.IL5, self.model.crew_value(crew, "IL5", hour), "pg/mL"),
                            (Analyte.IGE, self.model.crew_value(crew, "IGE", hour), "kU/L"),
                            (Analyte.EOSINOPHIL_ABS, self.model.crew_value(crew, "EOSINOPHIL_ABS", hour), "cel/uL"),
                            (Analyte.NEUTROPHIL_PCT, self.model.crew_value(crew, "NEUTROPHIL_PCT", hour), "%"),
                            (Analyte.LYMPHOCYTE_ABS, self.model.crew_value(crew, "LYMPHOCYTE_ABS", hour), "cel/uL"),
                            (Analyte.WBC, self.model.crew_value(crew, "WBC", hour), "cel/uL"),
                            (Analyte.SPO2, self.model.crew_value(crew, "SPO2", hour), "%"),
                        ],
                    ))
            day += timedelta(days=1)

        for ts, crew, sample_type, results in panels:
            if ts >= self.start + timedelta(hours=self.hours):
                continue
            payload = [
                (analyte, float(value), unit,
                 config.VIRAL_LOD_COPIES_ML if analyte.value.endswith("_DNA") else None)
                for analyte, value, unit in results
            ]
            repo.add_lab_sample_with_results(
                crew,
                sample_type,
                panel=f"PANEL_{sample_type.value}",
                collected_at=ts,
                results=payload,
                device_id="MINI-LAB-01",
            )
            self.stats.lab_samples += 1
            self.stats.biomarker_results += len(payload)

    # -- síntomas ---------------------------------------------------------

    def _generate_symptoms(self, repo: Repository) -> None:
        """
        Síntomas reportados por la tripulación.

        MS-05 desarrolla sibilancias al final (condición del Escenario C) y dos
        tripulantes refieren rinitis persistente por la exposición fúngica.
        """
        end = self.start + timedelta(hours=self.hours)
        if (end - self.start) < timedelta(hours=48):
            return
        events = [
            (0.80, "MS-05", "RHINORRHEA", 1),
            (0.88, "CDR-01", "FATIGUE", 2),
            (0.92, "MS-05", "WHEEZE", 2),
            (0.95, "PLT-02", "PHARYNGITIS", 1),
        ]
        for frac, crew, code, severity in events:
            ts = self.start + timedelta(hours=self.hours * frac)
            repo.add_symptom(crew, ts, code, severity=severity, source="SELF_REPORT")
            self.stats.symptoms += 1


def generate_mission(
    repo: Repository,
    *,
    hours: float = 96.0,
    interval_minutes: int = 2,
    seed: int = 20250301,
) -> GenerationStats:
    """Atajo funcional para ``SyntheticMission(...).generate(repo)``."""
    return SyntheticMission(hours=hours, interval_minutes=interval_minutes,
                            seed=seed).generate(repo)


__all__ = [
    "ANOMALY_FULL_H",
    "ANOMALY_START_H",
    "CREW_ENDPOINTS",
    "DegradationModel",
    "GenerationStats",
    "SIM_START",
    "SyntheticMission",
        "daily_wave",
    "generate_mission",
    "progression",
]

