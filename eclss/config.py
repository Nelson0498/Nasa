"""
eclss.config — Catálogo único de KPIs y configuración del módulo.

Módulo: «Entornos Cerrados Hostiles» (Operaciones Médicas Independientes de la
Tierra — misión a Marte, latencia de ida y vuelta 24–48 min).

Principio de diseño
-------------------
Este archivo es la **única fuente de verdad** de los umbrales. Ningún otro
módulo debe contener números mágicos: el generador sintético lee de aquí los
rangos nominales para producir telemetría plausible, y el motor de reglas lee
de aquí los cortes para clasificar severidad. Cuando el HRP revise un umbral
(p. ej. `ppCO2` crítico), se cambia en un solo lugar.

Cada umbral se versiona (`THRESHOLD_VERSION`) y se materializa en la tabla
`kpi_threshold` al sembrar la base de datos, de forma que toda alerta pueda
citar la versión exacta de los cortes que la dispararon (trazabilidad médica).

Procedencia de los valores
--------------------------
* ``HRP``     — derivado de los estándares de salud humana espacial y de la
                especificación funcional del módulo (rangos nominales dados).
* ``MVP-OPS`` — umbral operativo de este MVP cuando la especificación sólo da
                una dirección («sube TNF-α») y no una cifra. Marcado para
                recalibrar con datos reales de vuelo.

Unidades: SI + clínicas convencionales (kPa, °C, dBA, lux, UFC, nmol/L,
µg/mL, pg/mL, copias/mL, células/µL).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from enum import Enum
from typing import Final

# --------------------------------------------------------------------------- #
# 1. Enumeraciones de dominio
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    """Severidad única para señales ambientales, biomarcadores y alertas."""

    OK = "OK"
    WARNING = "WARNING"       # Amarillo: degradación, acción en < 2 h
    CRITICAL = "CRITICAL"     # Rojo carmesí: riesgo vital, acción en < 5 min
    UNKNOWN = "UNKNOWN"       # Dato ausente o inválido (nunca se ignora)

    @property
    def rank(self) -> int:
        """Ordinal para comparar y agregar severidades."""
        return _SEVERITY_RANK[self]

    @property
    def is_actionable(self) -> bool:
        return self in (Severity.WARNING, Severity.CRITICAL)


_SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.UNKNOWN: -1,
    Severity.OK: 0,
    Severity.WARNING: 1,
    Severity.CRITICAL: 2,
}


def worst(*severities: Severity | None) -> Severity:
    """Devuelve la severidad dominante (ignora ``None``)."""
    present = [s for s in severities if s is not None]
    return max(present, key=lambda s: s.rank) if present else Severity.UNKNOWN


class Direction(str, Enum):
    """Dirección de riesgo; documental, la clasificación es simétrica."""

    HIGH_BAD = "HIGH_BAD"
    LOW_BAD = "LOW_BAD"
    BAND = "BAND"


class SensorKind(str, Enum):
    """Canales de telemetría ECLSS continua."""

    PRESSURE_TOTAL = "PRESSURE_TOTAL"
    PPO2 = "PPO2"
    PPCO2 = "PPCO2"
    TEMPERATURE = "TEMPERATURE"
    HUMIDITY = "HUMIDITY"
    NOISE_LEQ = "NOISE_LEQ"              # Leq promediado 1 min
    NOISE_PEAK = "NOISE_PEAK"            # pico en la ventana de 1 min
    LIGHT_TOTAL = "LIGHT_TOTAL"          # lux totales en el módulo
    LIGHT_BLUE_480 = "LIGHT_BLUE_480"    # lux en banda 480 nm
    VOC_TVOC = "VOC_TVOC"                # ppb, apoyo a mVOCs microbianos


class SampleType(str, Enum):
    """Matriz biológica del laboratorio de a bordo."""

    SALIVA = "SALIVA"
    CAPILLARY_BLOOD = "CAPILLARY_BLOOD"
    URINE = "URINE"
    SURFACE_SWAB = "SURFACE_SWAB"
    AIR_IMPACTOR = "AIR_IMPACTOR"
    WATER = "WATER"


class Analyte(str, Enum):
    """Analitos del panel inmunológico / de estrés."""

    CORTISOL = "CORTISOL"                  # nmol/L
    SIGA = "SIGA"                          # µg/mL (saliva)
    EBV_DNA = "EBV_DNA"                    # copias/mL
    CMV_DNA = "CMV_DNA"                    # copias/mL
    VZV_DNA = "VZV_DNA"                    # copias/mL
    IL6 = "IL6"                            # pg/mL
    TNF_A = "TNF_A"                        # pg/mL
    IL10 = "IL10"                          # pg/mL
    IFN_G = "IFN_G"                        # pg/mL
    IL4 = "IL4"                            # pg/mL
    IL5 = "IL5"                            # pg/mL
    IGE = "IGE"                            # kU/L
    NEUTROPHIL_PCT = "NEUTROPHIL_PCT"      # %
    LYMPHOCYTE_ABS = "LYMPHOCYTE_ABS"      # células/µL
    EOSINOPHIL_ABS = "EOSINOPHIL_ABS"      # células/µL
    WBC = "WBC"                            # células/µL
    SPO2 = "SPO2"                          # %


class Route(str, Enum):
    """Rutas fisiopatológicas del motor de inferencia causal."""

    R1_CIRCADIAN_VIRAL = "R1_CIRCADIAN_VIRAL"
    R2_HUMIDITY_ALLERGY = "R2_HUMIDITY_ALLERGY"
    R3_BIOFILM_INFECTION = "R3_BIOFILM_INFECTION"


class Scenario(str, Enum):
    """Escenarios del protocolo de autonomía (árboles de decisión sin Tierra)."""

    A_WATER_IMMUNE = "A_WATER_IMMUNE"
    B_CIRCADIAN_VIRAL = "B_CIRCADIAN_VIRAL"
    C_FUNGAL_ALLERGY = "C_FUNGAL_ALLERGY"


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"   # ocultada por correlación de causa raíz


# --------------------------------------------------------------------------- #
# 2. Definición de umbral
# --------------------------------------------------------------------------- #

THRESHOLD_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class Threshold:
    """
    Corte de clasificación de un KPI.

    Todos los límites son opcionales: ``None`` significa «sin límite por ese
    lado». La clasificación es simétrica (alta y baja) porque varios KPI son
    peligrosos en ambos sentidos (``ppO2`` bajo = hipoxia, alto = riesgo de
    incendio y toxicidad pulmonar).

    Regla de precedencia: crítico > advertencia > nominal.
    """

    key: str
    unit: str
    direction: Direction = Direction.BAND
    nominal_low: float | None = None
    nominal_high: float | None = None
    warn_low: float | None = None
    warn_high: float | None = None
    crit_low: float | None = None
    crit_high: float | None = None
    source: str = "HRP"
    note: str = ""

    def classify(self, value: float | None) -> Severity:
        """Clasifica una lectura contra los cortes nominal/advertencia/crítico."""
        if value is None:
            return Severity.UNKNOWN
        if self.crit_high is not None and value > self.crit_high:
            return Severity.CRITICAL
        if self.crit_low is not None and value < self.crit_low:
            return Severity.CRITICAL
        if self.warn_high is not None and value > self.warn_high:
            return Severity.WARNING
        if self.warn_low is not None and value < self.warn_low:
            return Severity.WARNING
        return Severity.OK

    @property
    def nominal_range(self) -> tuple[float | None, float | None]:
        return (self.nominal_low, self.nominal_high)

    def describe(self) -> str:
        """Texto corto para la UI o el reporte (p. ej. ``<= 0.27 kPa``)."""
        parts: list[str] = []
        if self.nominal_low is not None:
            parts.append(f">= {self.nominal_low:g}")
        if self.nominal_high is not None:
            parts.append(f"<= {self.nominal_high:g}")
        joined = " y ".join(parts) if parts else "sin rango"
        return f"{joined} {self.unit}"


# --------------------------------------------------------------------------- #
# 3. Catálogo de KPIs (A. ambientales, B. microbiológicos, C. biomarcadores)
# --------------------------------------------------------------------------- #

KPI: Final[dict[str, Threshold]] = {
    # --- A. Parámetros ambientales físicos (telemetría ECLSS continua) -------
    "PRESSURE_TOTAL": Threshold(
        key="PRESSURE_TOTAL",
        unit="kPa",
        direction=Direction.BAND,
        nominal_low=70.3,
        nominal_high=101.3,
        warn_low=68.9,
        warn_high=102.7,
        crit_low=65.0,
        crit_high=None,  # la especificación no fija techo crítico
        source="HRP",
        note="< 65.0 kPa: riesgo de hipoxia y enfermedad por descompresión (DCS).",
    ),
    "PPO2": Threshold(
        key="PPO2",
        unit="kPa",
        direction=Direction.BAND,
        nominal_low=19.5,
        nominal_high=23.1,
        warn_low=18.5,
        warn_high=24.0,
        crit_low=17.0,
        crit_high=25.0,
        source="HRP",
        note="< 17 kPa hipoxia; > 25 kPa riesgo de incendio y toxicidad pulmonar.",
    ),
    "PPCO2": Threshold(
        key="PPCO2",
        unit="kPa",
        direction=Direction.HIGH_BAD,
        nominal_high=0.27,   # 2.0 mmHg
        # La banda amarilla de la especificación es 0.27–0.40 kPa. Se marca el
        # inicio de la advertencia en 0.27 (el techo nominal es ya el umbral de
        # deterioro cognitivo), no su extremo superior: si se usara 0.40, una
        # ppCO2 de 0.35 —que ya reduce la memoria de trabajo— se clasificaría
        # como nominal y el operador perdería la ventana de corrección.
        warn_high=0.27,      # 2.0 mmHg
        crit_high=0.53,      # 4.0 mmHg
        source="HRP",
        note=(
            "Banda amarilla 0.27–0.40 kPa (2.0–3.0 mmHg). Hipercapnia subclínica: "
            "reduce memoria de trabajo antes de dar síntomas."
        ),
    ),
    "TEMPERATURE": Threshold(
        key="TEMPERATURE",
        unit="C",
        direction=Direction.BAND,
        nominal_low=18.3,
        nominal_high=23.9,
        warn_low=17.0,
        warn_high=25.5,
        crit_low=15.0,
        crit_high=28.0,
        source="HRP",
    ),
    "HUMIDITY": Threshold(
        key="HUMIDITY",
        unit="%HR",
        direction=Direction.BAND,
        nominal_low=40.0,
        nominal_high=60.0,
        warn_low=30.0,
        warn_high=65.0,
        crit_low=25.0,   # irritación de mucosas
        crit_high=75.0,  # condensación y proliferación fúngica
        source="HRP",
    ),
    "NOISE_LEQ": Threshold(
        key="NOISE_LEQ",
        unit="dBA",
        direction=Direction.HIGH_BAD,
        nominal_high=60.0,   # 50 dBA descanso / 60 dBA trabajo
        warn_high=65.0,      # > 8 h de exposición
        crit_high=72.0,      # continuo
        source="HRP",
        note="Ruido nocturno > 62 dBA fragmenta la arquitectura del sueño.",
    ),
    "NOISE_PEAK": Threshold(
        key="NOISE_PEAK",
        unit="dBA",
        direction=Direction.HIGH_BAD,
        nominal_high=85.0,
        warn_high=85.0,
        crit_high=92.0,
        source="HRP",
        note="Picos > 85 dBA: riesgo auditivo y despertar abrupto.",
    ),
    "LIGHT_DAY": Threshold(
        key="LIGHT_DAY",
        unit="lux",
        direction=Direction.BAND,
        nominal_low=450.0,
        nominal_high=600.0,
        warn_low=360.0,    # desviación > 20 % del perfil programado
        warn_high=720.0,
        crit_low=200.0,
        crit_high=900.0,
        source="HRP",
        note="Perfil diurno enriquecido en azul (480 nm) para anclaje circadiano.",
    ),
    "LIGHT_NIGHT": Threshold(
        key="LIGHT_NIGHT",
        unit="lux",
        direction=Direction.HIGH_BAD,
        nominal_high=10.0,
        warn_high=20.0,
        crit_high=50.0,
        source="HRP",
        note="Fase de sueño: debe dominar el espectro ámbar (> 600 nm).",
    ),
    "BLUE_LIGHT_NIGHT": Threshold(
        key="BLUE_LIGHT_NIGHT",
        unit="lux@480nm",
        direction=Direction.HIGH_BAD,
        nominal_high=10.0,
        warn_high=50.0,    # umbral del Escenario B
        crit_high=100.0,   # supresión melatonínica franca
        source="HRP",
        note="> 50 lux en fase de sueño suprime melatonina y aplana el cortisol.",
    ),
    "LIGHT_SPECTRAL_DEVIATION": Threshold(
        key="LIGHT_SPECTRAL_DEVIATION",
        unit="%",
        direction=Direction.HIGH_BAD,
        nominal_high=20.0,
        warn_high=20.0,
        crit_high=40.0,
        source="HRP",
        note="Desviación respecto al perfil espectral programado.",
    ),
    "VOC_TVOC": Threshold(
        key="VOC_TVOC",
        unit="ppb",
        direction=Direction.HIGH_BAD,
        nominal_high=250.0,
        warn_high=500.0,
        crit_high=1000.0,
        source="MVP-OPS",
        note="Incluye mVOCs microbianos; apoyo a la Ruta 2.",
    ),
    # --- B. Parámetros microbiológicos --------------------------------------
    "AIR_BACTERIA": Threshold(
        key="AIR_BACTERIA",
        unit="UFC/m3",
        direction=Direction.HIGH_BAD,
        nominal_high=500.0,
        warn_high=500.0,
        crit_high=1000.0,
        source="HRP",
    ),
    "AIR_FUNGI": Threshold(
        key="AIR_FUNGI",
        unit="UFC/m3",
        direction=Direction.HIGH_BAD,
        nominal_high=50.0,
        warn_high=50.0,
        crit_high=100.0,
        source="HRP",
        note="Aspergillus/Penicillium disparan alerta inmediata sin importar el conteo.",
    ),
    "SURFACE_BIOLOAD": Threshold(
        key="SURFACE_BIOLOAD",
        unit="UFC/100cm2",
        direction=Direction.HIGH_BAD,
        nominal_high=100.0,
        warn_high=100.0,
        crit_high=1000.0,
        source="HRP",
    ),
    "WATER_TOC": Threshold(
        key="WATER_TOC",
        unit="mg/L",
        direction=Direction.HIGH_BAD,
        nominal_high=3.0,
        warn_high=3.0,
        crit_high=5.0,
        source="HRP",
        note="Indicador temprano de biopelícula en el WRS (Ruta 3).",
    ),
    "WATER_BACTERIA": Threshold(
        key="WATER_BACTERIA",
        unit="UFC/mL",
        direction=Direction.HIGH_BAD,
        nominal_high=50.0,
        warn_high=50.0,
        crit_high=100.0,
        source="HRP",
        note="Coliformes presentes => crítico con independencia del conteo.",
    ),
    # --- C. Biomarcadores (umbrales absolutos; los relativos viven abajo) ---
    "IL6": Threshold(
        key="IL6",
        unit="pg/mL",
        direction=Direction.HIGH_BAD,
        nominal_high=5.0,
        warn_high=5.0,
        crit_high=10.0,
        source="HRP",
        note="Elevación sostenida > 5 pg/mL = inflamación sistémica de bajo grado.",
    ),
    "EOSINOPHIL_ABS": Threshold(
        key="EOSINOPHIL_ABS",
        unit="cel/uL",
        direction=Direction.HIGH_BAD,
        nominal_high=300.0,
        warn_high=300.0,
        crit_high=500.0,
        source="HRP",
        note="> 500 cel/µL: hipersensibilidad / alergia ambiental (Ruta 2).",
    ),
    "NEUTROPHIL_PCT": Threshold(
        key="NEUTROPHIL_PCT",
        unit="%",
        direction=Direction.HIGH_BAD,
        nominal_high=65.0,
        warn_high=70.0,
        crit_high=75.0,
        source="HRP",
        note="Neutrofilia relativa; sólo cuenta como señal con linfopenia asociada.",
    ),
    "LYMPHOCYTE_ABS": Threshold(
        key="LYMPHOCYTE_ABS",
        unit="cel/uL",
        direction=Direction.LOW_BAD,
        nominal_low=1200.0,
        nominal_high=3500.0,
        warn_low=1200.0,
        crit_low=1000.0,
        source="HRP",
    ),
    "SPO2": Threshold(
        key="SPO2",
        unit="%",
        direction=Direction.LOW_BAD,
        nominal_low=96.0,
        warn_low=95.0,
        crit_low=94.0,
        source="HRP",
        note="< 94 % con sibilancias = condición del Escenario C.",
    ),
    "TNF_A": Threshold(
        key="TNF_A",
        unit="pg/mL",
        direction=Direction.HIGH_BAD,
        nominal_high=8.0,
        warn_high=8.0,
        crit_high=15.0,
        source="MVP-OPS",
        note="Sin cifra normativa en la especificación: umbral operativo a recalibrar.",
    ),
    "IGE": Threshold(
        key="IGE",
        unit="kU/L",
        direction=Direction.HIGH_BAD,
        nominal_high=100.0,
        warn_high=100.0,
        crit_high=200.0,
        source="MVP-OPS",
    ),
}


def kpi(key: str) -> Threshold:
    """Acceso tipado al catálogo (falla ruidosamente ante un typo)."""
    try:
        return KPI[key]
    except KeyError as exc:  # pragma: no cover - error de programación
        raise KeyError(f"KPI desconocido: {key!r}. Disponibles: {sorted(KPI)}") from exc


def severity_for(key: str, value: float | None) -> Severity:
    """Atajo: clasifica un valor contra el KPI indicado."""
    return kpi(key).classify(value)


# --------------------------------------------------------------------------- #
# 4. Parámetros inmunológicos relativos (línea base individual)
# --------------------------------------------------------------------------- #

#: Línea base de sIgA: > 100 µg/mL y ± 20 % respecto al valor pre-vuelo.
SIGA_BASELINE_MIN_UG_ML: Final[float] = 100.0
SIGA_DROP_WARN: Final[float] = 0.20   # caída relativa que ya es señal amarilla
SIGA_DROP_CRIT: Final[float] = 0.40   # caída que define vulnerabilidad mucosa

#: Cortisol salival: curva diurna normal y criterios de aplanamiento.
CORTISOL_AM_NOMINAL: Final[tuple[float, float]] = (10.0, 20.0)
CORTISOL_NIGHT_NOMINAL_MAX: Final[float] = 2.0
CORTISOL_NIGHT_CRIT: Final[float] = 5.0
CORTISOL_RATIO_WARN: Final[float] = 2.5   # mañana/noche
CORTISOL_RATIO_CRIT: Final[float] = 2.0   # aplanamiento de la curva

#: Carga viral latente (qPCR portátil: EBV, CMV, VZV).
VIRAL_LOD_COPIES_ML: Final[float] = 1e2     # límite de detección nominal
VIRAL_WARN_COPIES_ML: Final[float] = 1e3
VIRAL_CRIT_COPIES_ML: Final[float] = 1e4    # reactivación viral activa

#: Panel de citoquinas. La relación IFN-γ/IL-10 usa umbral operativo (MVP-OPS).
IFNG_IL10_NOMINAL_MIN: Final[float] = 1.0
IFNG_IL10_WARN: Final[float] = 0.8
IFNG_IL10_CRIT: Final[float] = 0.5

#: Microbiología: patógenos que escalan a crítico por sí solos.
OPPORTUNISTIC_PATHOGENS: Final[frozenset[str]] = frozenset(
    {"STAPHYLOCOCCUS_AUREUS", "MRSA", "PSEUDOMONAS_AERUGINOSA"}
)
MOLD_GENERA_ESCALATION: Final[frozenset[str]] = frozenset({"ASPERGILLUS", "PENICILLIUM"})


# --------------------------------------------------------------------------- #
# 5. Configuración del motor de reglas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """
    Parámetros del motor de correlación.

    ``window_hours`` define la ventana móvil de análisis; ``dedup_bucket_hours``
    agrupa ejecuciones consecutivas para no re-emitir la misma alerta.
    """

    window_hours: int = 24
    dedup_bucket_hours: int = 4
    rule_version: str = "1.0.0"

    # --- Ruta 1 / Escenario B: circadiano-acústico --------------------------
    noise_night_dba: float = 62.0        # dBA Leq nocturno que fragmenta el sueño
    noise_nights_required: int = 3       # noches consecutivas
    #: Un criterio de «3 noches consecutivas» no cabe en la ventana de 24 h:
    #: estas reglas declaran su propio lookback, independiente de `window_hours`.
    noise_lookback_hours: int = 72
    cortisol_lookback_days: int = 3
    blue_light_night_lux: float = 50.0   # lux @480 nm en fase de sueño
    cortisol_ratio_crit: float = CORTISOL_RATIO_CRIT
    cortisol_night_crit: float = CORTISOL_NIGHT_CRIT
    viral_copies_crit: float = VIRAL_CRIT_COPIES_ML

    # --- Ruta 2 / Escenario C: humedad-hongos -------------------------------
    humidity_sustained_pct: float = 70.0
    humidity_sustained_hours: float = 12.0
    fungi_crit_cfu_m3: float = 100.0
    eosinophil_crit: float = 500.0

    # --- Ruta 3 / Escenario A: biopelícula-hídrica --------------------------
    toc_crit_mg_l: float = 5.0
    water_bacteria_crit: float = 100.0
    siga_drop_crit: float = SIGA_DROP_CRIT
    il6_crit_pg_ml: float = 5.0
    il6_crew_required: int = 2           # >= 2 sujetos con IL-6 elevada

    # --- Ventanas circadianas (hora local del hábitat) ----------------------
    night_start: time = time(22, 0)
    night_end: time = time(6, 0)
    #: Desplazamiento fijo del reloj del hábitat respecto a UTC. En Marte el
    #: sol dura 24 h 39 min; el MVP trabaja con un offset fijo documentado.
    habitat_utc_offset_hours: float = 0.0


DEFAULT_ENGINE_CONFIG: Final[EngineConfig] = EngineConfig()


# --------------------------------------------------------------------------- #
# 6. Catálogo de sensores, módulos y tripulación (siembra + generador)
# --------------------------------------------------------------------------- #

#: (kind, unidad, hz_nominal, etiqueta física)
SENSOR_SPECS: Final[list[tuple[SensorKind, str, float, str]]] = [
    (SensorKind.PRESSURE_TOTAL, "kPa", 1.0, "Regulador de presión de cabina P1"),
    (SensorKind.PPO2, "kPa", 1.0, "Celda electroquímica O2 (puerto A)"),
    (SensorKind.PPCO2, "kPa", 0.5, "NDIR CO2 en retorno de cabina"),
    (SensorKind.TEMPERATURE, "C", 0.1, "RTD pared norte 1.5 m"),
    (SensorKind.HUMIDITY, "%HR", 0.1, "Capacitivo en retorno de aire"),
    (SensorKind.NOISE_LEQ, "dBA", 1 / 60, "Sonómetro promediado 1 min"),
    (SensorKind.NOISE_PEAK, "dBA", 1 / 60, "Pico máximo en la ventana de 1 min"),
    (SensorKind.LIGHT_TOTAL, "lux", 0.1, "Fotómetro cenital"),
    (SensorKind.LIGHT_BLUE_480, "lux@480nm", 0.1, "Fotómetro banda 480 nm"),
    (SensorKind.VOC_TVOC, "ppb", 1 / 60, "PID TVOC en retorno de aire"),
]

#: Módulos del hábitat del MVP: (código, nombre, volumen m3, tipo)
MODULE_SPECS: Final[list[tuple[str, str, float, str]]] = [
    ("HAB-01", "Módulo Habitacional", 112.0, "HAB"),
    ("LAB-01", "Módulo Laboratorio / Glovebox", 48.0, "LAB"),
    ("NODE-01", "Nodo de interconexión y galley", 32.0, "NODE"),
]

#: Tripulación: (código, rol, sexo, año nac., sIgA base µg/mL, cortisol AM base)
CREW_SPECS: Final[list[tuple[str, str, str, int, float, float]]] = [
    ("CDR-01", "Comandante / CDR", "F", 1984, 165.0, 16.5),
    ("PLT-02", "Piloto", "M", 1987, 142.0, 15.2),
    ("MS-03", "Especialista de misión (médico)", "F", 1990, 178.0, 17.8),
    ("MS-04", "Especialista de misión (ECLSS)", "M", 1992, 131.0, 14.6),
    ("MS-05", "Especialista de misión (ciencias)", "F", 1995, 121.0, 13.9),
]


# --------------------------------------------------------------------------- #
# 7. Helpers de ventana circadiana y de volcado de umbrales
# --------------------------------------------------------------------------- #


def is_night(ts_local: object, cfg: EngineConfig = DEFAULT_ENGINE_CONFIG) -> bool:
    """
    ¿La marca de tiempo cae en la fase de sueño?

    Acepta cualquier objeto con ``.time()`` (datetime de la stdlib o
    ``pandas.Timestamp``), lo que permite reutilizar la misma regla en el
    generador, el motor y el reporte.
    """
    local_time = ts_local.time()  # type: ignore[attr-defined]
    if cfg.night_start <= cfg.night_end:
        return cfg.night_start <= local_time < cfg.night_end
    # Ventana que cruza medianoche (caso habitual 22:00–06:00).
    return local_time >= cfg.night_start or local_time < cfg.night_end


def threshold_snapshot() -> list[dict[str, object]]:
    """
    Serializa el catálogo para la tabla ``kpi_threshold``.

    Permite auditar con qué cortes exactos se emitió cada alerta histórica.
    """
    return [
        {
            "kpi_key": th.key,
            "version": THRESHOLD_VERSION,
            "unit": th.unit,
            "direction": th.direction.value,
            "nominal_low": th.nominal_low,
            "nominal_high": th.nominal_high,
            "warn_low": th.warn_low,
            "warn_high": th.warn_high,
            "crit_low": th.crit_low,
            "crit_high": th.crit_high,
            "source": th.source,
            "note": th.note,
        }
        for th in KPI.values()
    ]


__all__ = [
    "AlertStatus",
    "Analyte",
    "CREW_SPECS",
    "CORTISOL_AM_NOMINAL",
    "CORTISOL_NIGHT_NOMINAL_MAX",
    "CORTISOL_RATIO_CRIT",
    "CORTISOL_RATIO_WARN",
    "DEFAULT_ENGINE_CONFIG",
    "Direction",
    "EngineConfig",
    "IFNG_IL10_CRIT",
    "IFNG_IL10_NOMINAL_MIN",
    "IFNG_IL10_WARN",
    "KPI",
    "MODULE_SPECS",
    "MOLD_GENERA_ESCALATION",
    "OPPORTUNISTIC_PATHOGENS",
    "Route",
    "SENSOR_SPECS",
    "SIGA_BASELINE_MIN_UG_ML",
    "SIGA_DROP_CRIT",
    "SIGA_DROP_WARN",
    "SampleType",
    "Scenario",
    "SensorKind",
    "Severity",
    "THRESHOLD_VERSION",
    "Threshold",
    "VIRAL_CRIT_COPIES_ML",
    "VIRAL_LOD_COPIES_ML",
    "VIRAL_WARN_COPIES_ML",
    "is_night",
    "kpi",
    "severity_for",
    "threshold_snapshot",
    "worst",
]
