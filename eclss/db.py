"""
eclss.db — Capa de acceso a datos (repositorio) sobre SQLite.

Se implementa con la librería estándar (``sqlite3``) para que el MVP corra
en cualquier portátil de la tripulación sin dependencias. La superficie pública
(``Repository``) es deliberadamente estrecha: insertar hechos inmutables y
consultar ventanas temporales. Todo el SQL vive aquí; el motor de reglas nunca
escribe SQL, sólo pide ventanas y persiste conclusiones.

Notas de diseño
---------------
* **Marcas de tiempo**: se guardan como texto ISO-8601 UTC con sufijo ``Z``.
  El formato es de ancho fijo, de modo que ``ORDER BY ts_utc`` y los rangos
  ``BETWEEN`` funcionan lexicográficamente sin funciones de fecha.
* **Idempotencia**: ``sensor_reading`` tiene ``UNIQUE(sensor_id, ts_utc)`` y se
  inserta con ``INSERT OR IGNORE``; volver a cargar el generador no duplica
  telemetría.
* **Inmutabilidad**: las tablas de hechos no tienen ``UPDATE`` ni ``DELETE``
  en esta API. Un resultado clínico erróneo se corrige emitiendo una muestra
  nueva (``status='REPEAT'``), no sobreescribiendo historia.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Base de datos por defecto del MVP (relativa al directorio de ejecución).
DEFAULT_DB_PATH = Path("out") / "eclss_mvp.sqlite3"

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


# --------------------------------------------------------------------------- #
# Utilidades de tiempo
# --------------------------------------------------------------------------- #


def to_iso(ts: datetime) -> str:
    """Normaliza a ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, sin microsegundos)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime(ISO_FMT)


def parse_iso(text: str) -> datetime:
    """Inverso de :func:`to_iso` (tolera ``+00:00`` y microsegundos)."""
    cleaned = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps(value: Any) -> str:
    """JSON compacto y determinista para columnas ``TEXT`` con JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# --------------------------------------------------------------------------- #
# Conexión e inicialización
# --------------------------------------------------------------------------- #


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Abre (o crea) la base y aplica el esquema.

    ``PRAGMA foreign_keys`` se activa por conexión: SQLite lo desactiva por
    omisión y sin él las FK declaradas en el esquema serían decorativas.
    """
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Ejecuta ``schema.sql`` (idempotente: todo es ``IF NOT EXISTS``)."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


# --------------------------------------------------------------------------- #
# Registros de referencia tipados
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CrewRecord:
    id: int
    code: str
    role: str
    baseline_siga_ug_ml: float
    baseline_cortisol_am: float | None
    baseline_cortisol_night: float | None
    terrestrial_allergy_hx: bool


@dataclass(frozen=True, slots=True)
class ModuleRecord:
    id: int
    code: str
    name: str
    kind: str


# --------------------------------------------------------------------------- #
# Repositorio
# --------------------------------------------------------------------------- #


class Repository:
    """
    Fachada de persistencia del módulo.

    Uso::

        repo = Repository(connect("out/eclss_mvp.sqlite3"))
        repo.seed(MissionSeed(...))
        rows = repo.env_series("PPCO2", start, end, module_code="HAB-01")
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- helpers internos --------------------------------------------------

    def _insert(self, table: str, row: dict[str, Any], *, or_ignore: bool = False) -> int:
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        verb = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
        cur = self.conn.execute(f"{verb} {table} ({cols}) VALUES ({marks})", row)
        return int(cur.lastrowid or 0)

    def commit(self) -> None:
        """
        Confirma el lote pendiente.

        Existe porque SQLite en modo WAL descarta lo no confirmado al cerrar la
        conexión: sin este commit, una caída del proceso a mitad de ingesta
        perdería la telemetría — y en un hábitat marciano no hay a quién pedirle
        los datos de nuevo. Cada método de escritura confirma su propio lote.
        """
        self.conn.commit()

    def _insert_many(
        self, table: str, rows: Sequence[dict[str, Any]], *, or_ignore: bool = False
    ) -> int:
        if not rows:
            return 0
        cols = ", ".join(rows[0])
        marks = ", ".join(f":{c}" for c in rows[0])
        verb = "INSERT OR IGNORE INTO" if or_ignore else "INSERT INTO"
        before = self.conn.total_changes
        self.conn.executemany(f"{verb} {table} ({cols}) VALUES ({marks})", rows)
        return self.conn.total_changes - before

    # -- siembra de referencia --------------------------------------------

    def seed(
        self,
        *,
        mission_code: str = "MARS-3Y-01",
        mission_name: str = "Misión Marte de 3 años — hábitat de superficie",
        launch_utc: datetime | None = None,
        comm_latency_min: int = 24,
        crew: Sequence[tuple] = config.CREW_SPECS,
        modules: Sequence[tuple] = config.MODULE_SPECS,
        sensors: Sequence[tuple] = config.SENSOR_SPECS,
    ) -> dict[str, int]:
        """
        Siembra referencia (misión, módulos, sensores, tripulación, umbrales).

        Es idempotente: si la misión ya existe, reutiliza sus identificadores y
        sólo completa lo que falte. Devuelve la misión creada/recuperada.
        """
        existing = self.conn.execute(
            "SELECT id FROM mission WHERE code = ?", (mission_code,)
        ).fetchone()
        if existing:
            mission_id = int(existing["id"])
        else:
            mission_id = self._insert(
                "mission",
                {
                    "code": mission_code,
                    "name": mission_name,
                    "launch_utc": to_iso(launch_utc or utcnow()),
                    "comm_latency_min": comm_latency_min,
                    "habitat_timezone": "UTC",
                },
            )

        # -- módulos + un sensor por (módulo, canal) ------------------------
        module_ids: dict[str, int] = {}
        for code, name, volume, kind in modules:
            row = self.conn.execute(
                "SELECT id FROM habitat_module WHERE code = ?", (code,)
            ).fetchone()
            if row:
                module_ids[code] = int(row["id"])
                continue
            module_ids[code] = self._insert(
                "habitat_module",
                {
                    "mission_id": mission_id,
                    "code": code,
                    "name": name,
                    "kind": kind,
                    "volume_m3": volume,
                },
            )

        for module_code, _name, _vol, _kind in modules:
            for kind, unit, hz, label in sensors:
                sensor_code = f"SEN-{module_code}-{kind.name}"
                self._insert(
                    "sensor",
                    {
                        "module_id": module_ids[module_code],
                        "code": sensor_code,
                        "kind": kind.value,
                        "unit": unit,
                        "location_label": f"{module_code}: {label}",
                        "sample_hz": hz,
                        "active": 1,
                    },
                    or_ignore=True,
                )

        # -- tripulación ---------------------------------------------------
        for code, role, sex, birth_year, siga, cortisol_am in crew:
            self._insert(
                "crew_member",
                {
                    "mission_id": mission_id,
                    "code": code,
                    "role": role,
                    "sex": sex,
                    "birth_year": birth_year,
                    "baseline_siga_ug_ml": siga,
                    "baseline_cortisol_am": cortisol_am,
                    "baseline_cortisol_night": round(cortisol_am / 9.0, 2),
                    "terrestrial_allergy_hx": 1 if code in {"MS-05"} else 0,
                    "active": 1,
                },
                or_ignore=True,
            )

        # -- catálogo de umbrales versionado -------------------------------
        self._insert_many("kpi_threshold", config.threshold_snapshot(), or_ignore=True)

        self.conn.commit()
        return {"mission_id": mission_id, **module_ids}

    # -- lectores de referencia -------------------------------------------

    def mission_id(self) -> int:
        row = self.conn.execute("SELECT id FROM mission ORDER BY id LIMIT 1").fetchone()
        if not row:
            raise LookupError("No hay misión sembrada. Llama antes a Repository.seed().")
        return int(row["id"])

    def crew(self) -> list[CrewRecord]:
        rows = self.conn.execute(
            "SELECT * FROM crew_member WHERE active = 1 ORDER BY code"
        ).fetchall()
        return [
            CrewRecord(
                id=int(r["id"]),
                code=str(r["code"]),
                role=str(r["role"]),
                baseline_siga_ug_ml=float(r["baseline_siga_ug_ml"] or 0.0),
                baseline_cortisol_am=r["baseline_cortisol_am"],
                baseline_cortisol_night=r["baseline_cortisol_night"],
                terrestrial_allergy_hx=bool(r["terrestrial_allergy_hx"]),
            )
            for r in rows
        ]

    def modules(self) -> list[ModuleRecord]:
        rows = self.conn.execute("SELECT * FROM habitat_module ORDER BY code").fetchall()
        return [
            ModuleRecord(
                id=int(r["id"]), code=str(r["code"]), name=str(r["name"]), kind=str(r["kind"])
            )
            for r in rows
        ]

    def sensor_id(self, module_code: str, kind: config.SensorKind) -> int:
        row = self.conn.execute(
            "SELECT id FROM sensor WHERE code = ?", (f"SEN-{module_code}-{kind.name}",)
        ).fetchone()
        if not row:
            raise LookupError(f"Sensor inexistente: {module_code}/{kind.name}")
        return int(row["id"])

    # -- escrituras de hechos ---------------------------------------------

    def add_readings(self, rows: Iterable[tuple[int, datetime, float, str]]) -> int:
        """
        Inserta telemetría: ``(sensor_id, ts, value, quality)``.

        Devuelve el número de filas efectivamente nuevas (las repetidas por la
        restricción ``UNIQUE`` se descartan silenciosamente).
        """
        payload = [
            {"sensor_id": sid, "ts_utc": to_iso(ts), "value": float(value), "quality": q}
            for sid, ts, value, q in rows
        ]
        written = self._insert_many("sensor_reading", payload, or_ignore=True)
        self.commit()
        return written

    def add_air_sample(
        self,
        module_code: str,
        sampled_at: datetime,
        *,
        bacteria_cfu_m3: float,
        fungi_cfu_m3: float,
        dominant_fungi: Sequence[str] = (),
        mvocs_ppb: float | None = None,
        method: str = "IMPACTOR_6STAGE",
        sampled_by: str | None = "MS-04",
    ) -> int:
        module = self._module_id(module_code)
        sample_id = self._insert(
            "air_microbio_sample",
            {
                "module_id": module,
                "sampled_at": to_iso(sampled_at),
                "bacteria_cfu_m3": bacteria_cfu_m3,
                "fungi_cfu_m3": fungi_cfu_m3,
                "dominant_fungi": dumps(list(dominant_fungi)),
                "mvocs_ppb": mvocs_ppb,
                "method": method,
                "sampled_by": sampled_by,
            },
        )
        self.commit()
        return sample_id

    def add_surface_swab(
        self,
        module_code: str,
        sampled_at: datetime,
        surface_label: str,
        *,
        total_cfu_per_100cm2: float,
        area_cm2: float = 100.0,
        staphylococcus_aureus: bool = False,
        mrsa: bool = False,
        pseudomonas_aeruginosa: bool = False,
        minion_pathogens: Sequence[str] = (),
        atp_rlu: float | None = None,
        sampled_by: str | None = None,
    ) -> int:
        module = self._module_id(module_code)
        swab_id = self._insert(
            "surface_swab",
            {
                "module_id": module,
                "sampled_at": to_iso(sampled_at),
                "surface_label": surface_label,
                "area_cm2": area_cm2,
                "total_cfu_per_100cm2": total_cfu_per_100cm2,
                "staphylococcus_aureus": int(staphylococcus_aureus),
                "mrsa": int(mrsa),
                "pseudomonas_aeruginosa": int(pseudomonas_aeruginosa),
                "minion_pathogens": dumps(list(minion_pathogens)),
                "atp_rlu": atp_rlu,
                "sampled_by": sampled_by,
            },
        )
        self.commit()
        return swab_id

    def add_water_sample(
        self,
        module_code: str,
        circuit: str,
        sampled_at: datetime,
        *,
        toc_mg_l: float,
        total_bacteria_cfu_ml: float,
        coliforms_present: bool = False,
        silver_ppb: float | None = None,
        turbidity_ntu: float | None = None,
        atp_rlu: float | None = None,
    ) -> int:
        module = self._module_id(module_code)
        water_id = self._insert(
            "water_quality_sample",
            {
                "module_id": module,
                "circuit": circuit,
                "sampled_at": to_iso(sampled_at),
                "toc_mg_l": toc_mg_l,
                "total_bacteria_cfu_ml": total_bacteria_cfu_ml,
                "coliforms_present": int(coliforms_present),
                "silver_ppb": silver_ppb,
                "turbidity_ntu": turbidity_ntu,
                "atp_rlu": atp_rlu,
            },
        )
        self.commit()
        return water_id

    def add_lab_sample_with_results(
        self,
        crew_code: str,
        sample_type: config.SampleType,
        panel: str,
        collected_at: datetime,
        results: Sequence[tuple[config.Analyte, float, str, float | None]],
        *,
        device_id: str = "MINI-LAB-01",
        status: str = "ANALYZED",
        notes: str = "",
    ) -> int:
        """
        Inserta una muestra y sus analitos en una sola unidad lógica.

        ``results``: ``(analyte, value, unit, lod)``.
        """
        crew_id = self._crew_id(crew_code)
        sample_id = self._insert(
            "lab_sample",
            {
                "crew_member_id": crew_id,
                "sample_type": sample_type.value,
                "panel": panel,
                "collected_at": to_iso(collected_at),
                "device_id": device_id,
                "status": status,
                "notes": notes,
            },
        )
        self._insert_many(
            "biomarker_result",
            [
                {
                    "lab_sample_id": sample_id,
                    "crew_member_id": crew_id,
                    "analyte": analyte.value,
                    "value": float(value),
                    "unit": unit,
                    "lod": lod,
                    "measured_at": to_iso(collected_at),
                    "flagged": 0,
                }
                for analyte, value, unit, lod in results
            ],
        )
        self.commit()
        return sample_id

    def add_symptom(
        self,
        crew_code: str,
        reported_at: datetime,
        symptom_code: str,
        *,
        severity: int = 1,
        source: str = "SELF_REPORT",
        notes: str = "",
    ) -> int:
        symptom_id = self._insert(
            "symptom_report",
            {
                "crew_member_id": self._crew_id(crew_code),
                "reported_at": to_iso(reported_at),
                "symptom_code": symptom_code,
                "severity": severity,
                "source": source,
                "notes": notes,
            },
        )
        self.commit()
        return symptom_id

    def save_alert(self, alert: dict[str, Any], evidence: Sequence[dict[str, Any]] = (),
                   actions: Sequence[dict[str, Any]] = ()) -> int:
        """
        Persiste una alerta con su evidencia y su plan de acciones.

        La alerta es idempotente por ``dedup_key``: si ya existe se devuelve su
        ``id`` sin reescribir nada (la evidencia original se conserva).
        """
        existing = self.conn.execute(
            "SELECT id FROM alert WHERE dedup_key = ?", (alert["dedup_key"],)
        ).fetchone()
        if existing:
            return int(existing["id"])

        alert_id = self._insert("alert", alert)
        self._insert_many(
            "alert_evidence",
            [{**ev, "alert_id": alert_id} for ev in evidence],
        )
        self._insert_many(
            "autonomous_action",
            [{**act, "alert_id": alert_id} for act in actions],
        )
        # Alerta, evidencia y acciones se confirman juntas: nunca debe existir
        # una alerta visible sin su justificación clínica.
        self.commit()
        return alert_id

    def log_rule_execution(self, row: dict[str, Any]) -> int:
        execution_id = self._insert("rule_execution", row)
        self.commit()
        return execution_id

    def queue_downlink(
        self,
        kind: str,
        payload: Any,
        *,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
    ) -> int:
        blob = dumps(payload)
        packet_id = self._insert(
            "downlink_packet",
            {
                "created_at": to_iso(utcnow()),
                "kind": kind,
                "window_from": to_iso(window_from) if window_from else None,
                "window_to": to_iso(window_to) if window_to else None,
                "payload_json": blob,
                "bytes": len(blob.encode("utf-8")),
                "status": "QUEUED",
            },
        )
        self.commit()
        return packet_id

    # -- lectores de hechos (ventanas temporales) --------------------------

    def _module_id(self, module_code: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM habitat_module WHERE code = ?", (module_code,)
        ).fetchone()
        if not row:
            raise LookupError(f"Módulo inexistente: {module_code}")
        return int(row["id"])

    def _crew_id(self, crew_code: str) -> int:
        row = self.conn.execute(
            "SELECT id FROM crew_member WHERE code = ?", (crew_code,)
        ).fetchone()
        if not row:
            raise LookupError(f"Tripulante inexistente: {crew_code}")
        return int(row["id"])

    def env_series(
        self,
        kind: config.SensorKind | str,
        start: datetime,
        end: datetime,
        module_code: str | None = None,
    ) -> list[tuple[datetime, float]]:
        """
        Serie temporal ``(ts, value)`` de un canal ECLSS en ``[start, end]``.

        Si ``module_code`` es ``None`` se agregan todos los módulos (el sensor
        representativo del hábitat es HAB-01; para el MVP cada módulo tiene su
        propio sensor, así que el filtro por módulo es lo habitual).
        """
        kind_value = kind.value if isinstance(kind, config.SensorKind) else kind
        sql = [
            "SELECT r.ts_utc AS ts, r.value AS value",
            "FROM sensor_reading r JOIN sensor s ON s.id = r.sensor_id",
            "WHERE s.kind = ? AND r.ts_utc BETWEEN ? AND ?",
        ]
        params: list[Any] = [kind_value, to_iso(start), to_iso(end)]
        if module_code:
            sql.append("AND s.module_id = ?")
            params.append(self._module_id(module_code))
        sql.append("ORDER BY r.ts_utc")
        rows = self.conn.execute(" ".join(sql), params).fetchall()
        return [(parse_iso(r["ts"]), float(r["value"])) for r in rows]

    def air_samples(self, start: datetime, end: datetime,
                    module_code: str | None = None) -> list[dict[str, Any]]:
        return self._fetch_window("air_microbio_sample", "sampled_at", start, end,
                                  module_code)

    def surface_swabs(self, start: datetime, end: datetime,
                      module_code: str | None = None) -> list[dict[str, Any]]:
        return self._fetch_window("surface_swab", "sampled_at", start, end, module_code)

    def water_samples(self, start: datetime, end: datetime,
                      module_code: str | None = None) -> list[dict[str, Any]]:
        return self._fetch_window("water_quality_sample", "sampled_at", start, end,
                                  module_code)

    def _fetch_window(self, table: str, ts_col: str, start: datetime, end: datetime,
                      module_code: str | None) -> list[dict[str, Any]]:
        sql = [f"SELECT t.*, m.code AS module_code FROM {table} t",
               "JOIN habitat_module m ON m.id = t.module_id",
               f"WHERE t.{ts_col} BETWEEN ? AND ?"]
        params: list[Any] = [to_iso(start), to_iso(end)]
        if module_code:
            sql.append("AND m.code = ?")
            params.append(module_code)
        sql.append(f"ORDER BY t.{ts_col}")
        return [dict(r) for r in self.conn.execute(" ".join(sql), params).fetchall()]

    def biomarker_series(
        self,
        analyte: config.Analyte | str,
        start: datetime,
        end: datetime,
    ) -> dict[str, list[tuple[datetime, float]]]:
        """
        Resultados de un analito agrupados por código de tripulante.

        Devuelve ``{crew_code: [(ts, value), ...]}`` ordenado por tiempo.
        """
        analyte_value = analyte.value if isinstance(analyte, config.Analyte) else analyte
        rows = self.conn.execute(
            """
            SELECT c.code AS crew_code, b.measured_at AS ts, b.value AS value
            FROM biomarker_result b
            JOIN crew_member c ON c.id = b.crew_member_id
            WHERE b.analyte = ? AND b.measured_at BETWEEN ? AND ?
            ORDER BY c.code, b.measured_at
            """,
            (analyte_value, to_iso(start), to_iso(end)),
        ).fetchall()
        out: dict[str, list[tuple[datetime, float]]] = {}
        for r in rows:
            out.setdefault(str(r["crew_code"]), []).append(
                (parse_iso(r["ts"]), float(r["value"]))
            )
        return out

    def symptoms(self, start: datetime, end: datetime) -> dict[str, set[str]]:
        """``{crew_code: {symptom_code, ...}}`` dentro de la ventana."""
        rows = self.conn.execute(
            """
            SELECT c.code AS crew_code, s.symptom_code AS code
            FROM symptom_report s JOIN crew_member c ON c.id = s.crew_member_id
            WHERE s.reported_at BETWEEN ? AND ?
            """,
            (to_iso(start), to_iso(end)),
        ).fetchall()
        out: dict[str, set[str]] = {}
        for r in rows:
            out.setdefault(str(r["crew_code"]), set()).add(str(r["code"]))
        return out

    def time_bounds(self) -> tuple[datetime, datetime] | None:
        """Extremos temporales de la telemetría (útil para reportes)."""
        row = self.conn.execute(
            "SELECT MIN(ts_utc) AS lo, MAX(ts_utc) AS hi FROM sensor_reading"
        ).fetchone()
        if not row or not row["lo"]:
            return None
        return parse_iso(row["lo"]), parse_iso(row["hi"])

    def count(self, table: str) -> int:
        """Conteo rápido para verificaciones y reportes."""
        return int(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])

    def alerts(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alert"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY created_at, id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def actions_for(self, alert_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM autonomous_action WHERE alert_id = ? ORDER BY sequence",
                (alert_id,),
            ).fetchall()
        ]

    def evidence_for(self, alert_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM alert_evidence WHERE alert_id = ? ORDER BY id",
                (alert_id,),
            ).fetchall()
        ]


# --------------------------------------------------------------------------- #
# Helpers de ventana temporal reutilizables
# --------------------------------------------------------------------------- #


def last_hours(end: datetime, hours: float) -> datetime:
    return end - timedelta(hours=hours)


def night_slices(
    start: datetime, end: datetime, cfg: config.EngineConfig = config.DEFAULT_ENGINE_CONFIG
) -> list[tuple[datetime, datetime]]:
    """
    Particiona ``[start, end]`` en los intervalos que caen en fase de sueño.

    Se usa para calcular el Leq nocturno y la exposición a luz azul nocturna
    sin depender de ``pandas.resample`` (mantiene el motor libre de pandas).
    """
    offset = timedelta(hours=cfg.habitat_utc_offset_hours)
    slices: list[tuple[datetime, datetime]] = []
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        nxt = cursor + timedelta(hours=1)
        local = cursor + offset
        if config.is_night(local, cfg) and local.date() == (cursor + offset).date():
            slices.append((max(cursor, start), min(nxt, end)))
        cursor = nxt
    return [s for s in slices if s[0] < s[1]]
