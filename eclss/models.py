"""
eclss.models — Modelos ORM (SQLAlchemy 2.0) equivalentes a ``schema.sql``.

Relación con el resto del sistema
---------------------------------
``schema.sql`` es la **fuente canónica** del esquema: el MVP corre sobre
``sqlite3`` puro y no necesita SQLAlchemy. Este módulo existe para el salto a
producción (PostgreSQL a bordo / centro de control) y para que el equipo pueda
trabajar con el ORM si lo prefiere.

Para evitar la deriva clásica entre «el DDL» y «los modelos», todas las
columnas declaran **explícitamente** su nombre físico como primer argumento de
``mapped_column``. Eso permite que ``tests/test_schema_parity.py`` compare, sin
necesidad de SQLAlchemy instalado, los nombres de tabla y columna del DDL con
los declarados aquí. Si alguien añade una columna al SQL y olvida el modelo (o
al revés), el test falla.

Uso::

    from sqlalchemy import create_engine
    from eclss.models import Base

    engine = create_engine("postgresql+psycopg://user:pass@host/eclss")
    Base.metadata.create_all(engine)

Nota: SQLAlchemy es una dependencia **opcional** del MVP; este módulo sólo se
importa cuando está instalado.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarativa común."""


# --------------------------------------------------------------------------- #
# 1. Referencia
# --------------------------------------------------------------------------- #


class Mission(Base):
    __tablename__ = "mission"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column("code", String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column("name", Text, nullable=False)
    launch_utc: Mapped[Optional[str]] = mapped_column("launch_utc", String)
    comm_latency_min: Mapped[int] = mapped_column(
        "comm_latency_min", Integer, nullable=False, default=24
    )
    habitat_timezone: Mapped[str] = mapped_column(
        "habitat_timezone", String, nullable=False, default="UTC"
    )
    created_at: Mapped[Optional[datetime]] = mapped_column("created_at", String)

    modules: Mapped[list["HabitatModule"]] = relationship(back_populates="mission")
    crew: Mapped[list["CrewMember"]] = relationship(back_populates="mission")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="mission")


class HabitatModule(Base):
    __tablename__ = "habitat_module"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[int] = mapped_column(
        "mission_id", ForeignKey("mission.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column("code", String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column("name", Text, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    volume_m3: Mapped[float] = mapped_column("volume_m3", Float, nullable=False)

    mission: Mapped["Mission"] = relationship(back_populates="modules")
    sensors: Mapped[list["Sensor"]] = relationship(back_populates="module")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('HAB','LAB','NODE','AIRLOCK','GREENHOUSE')", name="ck_module_kind"
        ),
    )


class CrewMember(Base):
    __tablename__ = "crew_member"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[int] = mapped_column(
        "mission_id", ForeignKey("mission.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column("code", String, nullable=False, unique=True)
    role: Mapped[str] = mapped_column("role", Text, nullable=False)
    sex: Mapped[Optional[str]] = mapped_column("sex", String)
    birth_year: Mapped[Optional[int]] = mapped_column("birth_year", Integer)
    # Línea base pre-vuelo: sin ella no existen umbrales relativos (sIgA, cortisol)
    baseline_siga_ug_ml: Mapped[Optional[float]] = mapped_column(
        "baseline_siga_ug_ml", Float
    )
    baseline_cortisol_am: Mapped[Optional[float]] = mapped_column(
        "baseline_cortisol_am", Float
    )
    baseline_cortisol_night: Mapped[Optional[float]] = mapped_column(
        "baseline_cortisol_night", Float
    )
    terrestrial_allergy_hx: Mapped[int] = mapped_column(
        "terrestrial_allergy_hx", Integer, nullable=False, default=0
    )
    active: Mapped[int] = mapped_column("active", Integer, nullable=False, default=1)

    mission: Mapped["Mission"] = relationship(back_populates="crew")
    samples: Mapped[list["LabSample"]] = relationship(back_populates="crew_member")
    biomarkers: Mapped[list["BiomarkerResult"]] = relationship(
        back_populates="crew_member"
    )
    symptoms: Mapped[list["SymptomReport"]] = relationship(back_populates="crew_member")

    __table_args__ = (
        CheckConstraint("sex IN ('F','M','X')", name="ck_crew_sex"),
        CheckConstraint("terrestrial_allergy_hx IN (0,1)", name="ck_crew_allergy"),
        CheckConstraint("active IN (0,1)", name="ck_crew_active"),
    )


class Sensor(Base):
    __tablename__ = "sensor"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(
        "module_id", ForeignKey("habitat_module.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column("code", String, nullable=False, unique=True)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    unit: Mapped[str] = mapped_column("unit", String, nullable=False)
    location_label: Mapped[Optional[str]] = mapped_column("location_label", Text)
    sample_hz: Mapped[float] = mapped_column("sample_hz", Float, nullable=False)
    active: Mapped[int] = mapped_column("active", Integer, nullable=False, default=1)

    module: Mapped["HabitatModule"] = relationship(back_populates="sensors")
    readings: Mapped[list["SensorReading"]] = relationship(back_populates="sensor")

    __table_args__ = (
        CheckConstraint("sample_hz > 0", name="ck_sensor_hz"),
        CheckConstraint("active IN (0,1)", name="ck_sensor_active"),
    )


class KpiThreshold(Base):
    """Fotografía versionada del catálogo de umbrales (trazabilidad médica)."""

    __tablename__ = "kpi_threshold"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    kpi_key: Mapped[str] = mapped_column("kpi_key", String, nullable=False)
    version: Mapped[int] = mapped_column("version", Integer, nullable=False)
    unit: Mapped[str] = mapped_column("unit", String, nullable=False)
    direction: Mapped[str] = mapped_column("direction", String, nullable=False)
    nominal_low: Mapped[Optional[float]] = mapped_column("nominal_low", Float)
    nominal_high: Mapped[Optional[float]] = mapped_column("nominal_high", Float)
    warn_low: Mapped[Optional[float]] = mapped_column("warn_low", Float)
    warn_high: Mapped[Optional[float]] = mapped_column("warn_high", Float)
    crit_low: Mapped[Optional[float]] = mapped_column("crit_low", Float)
    crit_high: Mapped[Optional[float]] = mapped_column("crit_high", Float)
    source: Mapped[str] = mapped_column("source", String, nullable=False, default="HRP")
    note: Mapped[str] = mapped_column("note", Text, nullable=False, default="")

    __table_args__ = (UniqueConstraint("kpi_key", "version", name="uq_kpi_version"),)


# --------------------------------------------------------------------------- #
# 2. Telemetría ECLSS continua
# --------------------------------------------------------------------------- #


class SensorReading(Base):
    __tablename__ = "sensor_reading"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    sensor_id: Mapped[int] = mapped_column(
        "sensor_id", ForeignKey("sensor.id", ondelete="CASCADE"), nullable=False
    )
    ts_utc: Mapped[str] = mapped_column("ts_utc", String, nullable=False)
    value: Mapped[float] = mapped_column("value", Float, nullable=False)
    quality: Mapped[str] = mapped_column("quality", String, nullable=False, default="OK")
    ingested_at: Mapped[Optional[str]] = mapped_column("ingested_at", String)

    sensor: Mapped["Sensor"] = relationship(back_populates="readings")

    __table_args__ = (
        UniqueConstraint("sensor_id", "ts_utc", name="uq_reading_sensor_ts"),
        CheckConstraint(
            "quality IN ('OK','DOUBTFUL','INVALID','CALIBRATING')", name="ck_reading_quality"
        ),
    )


# --------------------------------------------------------------------------- #
# 3. Microbiología
# --------------------------------------------------------------------------- #


class AirMicrobioSample(Base):
    __tablename__ = "air_microbio_sample"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(
        "module_id", ForeignKey("habitat_module.id", ondelete="CASCADE"), nullable=False
    )
    sampled_at: Mapped[str] = mapped_column("sampled_at", String, nullable=False)
    bacteria_cfu_m3: Mapped[float] = mapped_column("bacteria_cfu_m3", Float, nullable=False)
    fungi_cfu_m3: Mapped[float] = mapped_column("fungi_cfu_m3", Float, nullable=False)
    dominant_fungi: Mapped[str] = mapped_column(
        "dominant_fungi", Text, nullable=False, default="[]"
    )
    mvocs_ppb: Mapped[Optional[float]] = mapped_column("mvocs_ppb", Float)
    method: Mapped[str] = mapped_column(
        "method", String, nullable=False, default="IMPACTOR_6STAGE"
    )
    sampled_by: Mapped[Optional[str]] = mapped_column("sampled_by", String)

    __table_args__ = (
        CheckConstraint("bacteria_cfu_m3 >= 0", name="ck_air_bact"),
        CheckConstraint("fungi_cfu_m3 >= 0", name="ck_air_fungi"),
    )


class SurfaceSwab(Base):
    __tablename__ = "surface_swab"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(
        "module_id", ForeignKey("habitat_module.id", ondelete="CASCADE"), nullable=False
    )
    sampled_at: Mapped[str] = mapped_column("sampled_at", String, nullable=False)
    surface_label: Mapped[str] = mapped_column("surface_label", String, nullable=False)
    area_cm2: Mapped[float] = mapped_column("area_cm2", Float, nullable=False, default=100.0)
    total_cfu_per_100cm2: Mapped[float] = mapped_column(
        "total_cfu_per_100cm2", Float, nullable=False
    )
    staphylococcus_aureus: Mapped[int] = mapped_column(
        "staphylococcus_aureus", Integer, nullable=False, default=0
    )
    mrsa: Mapped[int] = mapped_column("mrsa", Integer, nullable=False, default=0)
    pseudomonas_aeruginosa: Mapped[int] = mapped_column(
        "pseudomonas_aeruginosa", Integer, nullable=False, default=0
    )
    minion_pathogens: Mapped[str] = mapped_column(
        "minion_pathogens", Text, nullable=False, default="[]"
    )
    atp_rlu: Mapped[Optional[float]] = mapped_column("atp_rlu", Float)
    sampled_by: Mapped[Optional[str]] = mapped_column("sampled_by", String)

    __table_args__ = (
        CheckConstraint("area_cm2 > 0", name="ck_swab_area"),
        CheckConstraint("total_cfu_per_100cm2 >= 0", name="ck_swab_cfu"),
        CheckConstraint("staphylococcus_aureus IN (0,1)", name="ck_swab_sa"),
        CheckConstraint("mrsa IN (0,1)", name="ck_swab_mrsa"),
        CheckConstraint("pseudomonas_aeruginosa IN (0,1)", name="ck_swab_pa"),
    )


class WaterQualitySample(Base):
    __tablename__ = "water_quality_sample"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[int] = mapped_column(
        "module_id", ForeignKey("habitat_module.id", ondelete="CASCADE"), nullable=False
    )
    circuit: Mapped[str] = mapped_column("circuit", String, nullable=False)
    sampled_at: Mapped[str] = mapped_column("sampled_at", String, nullable=False)
    toc_mg_l: Mapped[float] = mapped_column("toc_mg_l", Float, nullable=False)
    total_bacteria_cfu_ml: Mapped[float] = mapped_column(
        "total_bacteria_cfu_ml", Float, nullable=False
    )
    coliforms_present: Mapped[int] = mapped_column(
        "coliforms_present", Integer, nullable=False, default=0
    )
    silver_ppb: Mapped[Optional[float]] = mapped_column("silver_ppb", Float)
    turbidity_ntu: Mapped[Optional[float]] = mapped_column("turbidity_ntu", Float)
    atp_rlu: Mapped[Optional[float]] = mapped_column("atp_rlu", Float)

    __table_args__ = (
        CheckConstraint("circuit IN ('A','B')", name="ck_water_circuit"),
        CheckConstraint("toc_mg_l >= 0", name="ck_water_toc"),
        CheckConstraint("total_bacteria_cfu_ml >= 0", name="ck_water_bact"),
        CheckConstraint("coliforms_present IN (0,1)", name="ck_water_coliforms"),
    )


# --------------------------------------------------------------------------- #
# 4. Laboratorio clínico
# --------------------------------------------------------------------------- #


class LabSample(Base):
    __tablename__ = "lab_sample"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    crew_member_id: Mapped[int] = mapped_column(
        "crew_member_id", ForeignKey("crew_member.id", ondelete="CASCADE"), nullable=False
    )
    sample_type: Mapped[str] = mapped_column("sample_type", String, nullable=False)
    panel: Mapped[str] = mapped_column("panel", String, nullable=False)
    collected_at: Mapped[str] = mapped_column("collected_at", String, nullable=False)
    device_id: Mapped[str] = mapped_column("device_id", String, nullable=False)
    status: Mapped[str] = mapped_column(
        "status", String, nullable=False, default="ANALYZED"
    )
    notes: Mapped[str] = mapped_column("notes", Text, nullable=False, default="")

    crew_member: Mapped["CrewMember"] = relationship(back_populates="samples")
    results: Mapped[list["BiomarkerResult"]] = relationship(back_populates="sample")

    __table_args__ = (
        CheckConstraint(
            "status IN ('COLLECTED','ANALYZED','VOID','REPEAT')", name="ck_lab_status"
        ),
    )


class BiomarkerResult(Base):
    """
    Un analito por fila (modelo EAV).

    Los paneles cambian entre misiones y los analitos son heterogéneos (nmol/L,
    copias/mL, células/µL); una tabla ancha obligaría a migrar el esquema en cada
    nuevo biomarcador.
    """

    __tablename__ = "biomarker_result"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    lab_sample_id: Mapped[int] = mapped_column(
        "lab_sample_id", ForeignKey("lab_sample.id", ondelete="CASCADE"), nullable=False
    )
    crew_member_id: Mapped[int] = mapped_column(
        "crew_member_id", ForeignKey("crew_member.id", ondelete="CASCADE"), nullable=False
    )
    analyte: Mapped[str] = mapped_column("analyte", String, nullable=False)
    value: Mapped[float] = mapped_column("value", Float, nullable=False)
    unit: Mapped[str] = mapped_column("unit", String, nullable=False)
    lod: Mapped[Optional[float]] = mapped_column("lod", Float)
    measured_at: Mapped[str] = mapped_column("measured_at", String, nullable=False)
    flagged: Mapped[int] = mapped_column("flagged", Integer, nullable=False, default=0)

    sample: Mapped["LabSample"] = relationship(back_populates="results")
    crew_member: Mapped["CrewMember"] = relationship(back_populates="biomarkers")

    __table_args__ = (
        UniqueConstraint("lab_sample_id", "analyte", name="uq_result_sample_analyte"),
        CheckConstraint("flagged IN (0,1)", name="ck_result_flagged"),
    )


# --------------------------------------------------------------------------- #
# 5. Síntomas
# --------------------------------------------------------------------------- #


class SymptomReport(Base):
    __tablename__ = "symptom_report"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    crew_member_id: Mapped[int] = mapped_column(
        "crew_member_id", ForeignKey("crew_member.id", ondelete="CASCADE"), nullable=False
    )
    reported_at: Mapped[str] = mapped_column("reported_at", String, nullable=False)
    symptom_code: Mapped[str] = mapped_column("symptom_code", String, nullable=False)
    severity: Mapped[int] = mapped_column("severity", Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(
        "source", String, nullable=False, default="SELF_REPORT"
    )
    notes: Mapped[str] = mapped_column("notes", Text, nullable=False, default="")

    crew_member: Mapped["CrewMember"] = relationship(back_populates="symptoms")

    __table_args__ = (CheckConstraint("severity BETWEEN 1 AND 3", name="ck_symptom_sev"),)


# --------------------------------------------------------------------------- #
# 6. Alertas, evidencia y acciones
# --------------------------------------------------------------------------- #


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    mission_id: Mapped[int] = mapped_column(
        "mission_id", ForeignKey("mission.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column("created_at", String, nullable=False)
    rule_id: Mapped[str] = mapped_column("rule_id", String, nullable=False)
    rule_version: Mapped[str] = mapped_column("rule_version", String, nullable=False)
    route: Mapped[Optional[str]] = mapped_column("route", String)
    scenario: Mapped[Optional[str]] = mapped_column("scenario", String)
    severity: Mapped[str] = mapped_column("severity", String, nullable=False)
    category: Mapped[str] = mapped_column("category", String, nullable=False)
    title: Mapped[str] = mapped_column("title", Text, nullable=False)
    clinical_impact: Mapped[str] = mapped_column("clinical_impact", Text, nullable=False)
    status: Mapped[str] = mapped_column("status", String, nullable=False, default="OPEN")
    dedup_key: Mapped[str] = mapped_column("dedup_key", String, nullable=False)
    root_cause_alert_id: Mapped[Optional[int]] = mapped_column(
        "root_cause_alert_id", ForeignKey("alert.id", ondelete="SET NULL")
    )
    suppressed_count: Mapped[int] = mapped_column(
        "suppressed_count", Integer, nullable=False, default=0
    )
    risk_score: Mapped[float] = mapped_column("risk_score", Float, nullable=False, default=0.0)
    threshold_version: Mapped[int] = mapped_column(
        "threshold_version", Integer, nullable=False
    )
    window_start: Mapped[Optional[str]] = mapped_column("window_start", String)
    window_end: Mapped[Optional[str]] = mapped_column("window_end", String)

    mission: Mapped["Mission"] = relationship(back_populates="alerts")
    root_cause: Mapped[Optional["Alert"]] = relationship(
        back_populates="derived", remote_side=[id]
    )
    derived: Mapped[list["Alert"]] = relationship(back_populates="root_cause")
    evidence: Mapped[list["AlertEvidence"]] = relationship(back_populates="alert")
    actions: Mapped[list["AutonomousAction"]] = relationship(back_populates="alert")

    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_alert_dedup"),
        CheckConstraint(
            "severity IN ('INFO','WARNING','CRITICAL')", name="ck_alert_severity"
        ),
        CheckConstraint(
            "category IN ('ENV','MICRO','IMMUNO','INTEGRATED','SYSTEM')",
            name="ck_alert_category",
        ),
        CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS','RESOLVED','SUPPRESSED')",
            name="ck_alert_status",
        ),
        CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_alert_score"),
    )


class AlertEvidence(Base):
    __tablename__ = "alert_evidence"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        "alert_id", ForeignKey("alert.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    ref_table: Mapped[str] = mapped_column("ref_table", String, nullable=False)
    ref_id: Mapped[Optional[int]] = mapped_column("ref_id", Integer)
    metric: Mapped[str] = mapped_column("metric", String, nullable=False)
    observed_value: Mapped[Optional[float]] = mapped_column("observed_value", Float)
    threshold_value: Mapped[Optional[float]] = mapped_column("threshold_value", Float)
    unit: Mapped[Optional[str]] = mapped_column("unit", String)
    contribution: Mapped[float] = mapped_column(
        "contribution", Float, nullable=False, default=0.0
    )

    alert: Mapped["Alert"] = relationship(back_populates="evidence")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('ENV','MICRO','BIOMARKER','SYMPTOM')", name="ck_evidence_kind"
        ),
    )


class AutonomousAction(Base):
    __tablename__ = "autonomous_action"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        "alert_id", ForeignKey("alert.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column("sequence", Integer, nullable=False)
    phase: Mapped[str] = mapped_column("phase", String, nullable=False)
    action_type: Mapped[str] = mapped_column("action_type", String, nullable=False)
    description: Mapped[str] = mapped_column("description", Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(
        "payload_json", Text, nullable=False, default="{}"
    )
    requires_human_confirmation: Mapped[int] = mapped_column(
        "requires_human_confirmation", Integer, nullable=False, default=1
    )
    verification_type: Mapped[Optional[str]] = mapped_column("verification_type", String)
    qr_code: Mapped[Optional[str]] = mapped_column("qr_code", String)
    storage_location: Mapped[Optional[str]] = mapped_column("storage_location", String)
    status: Mapped[str] = mapped_column(
        "status", String, nullable=False, default="PENDING"
    )
    executed_at: Mapped[Optional[str]] = mapped_column("executed_at", String)
    executed_by: Mapped[Optional[str]] = mapped_column("executed_by", String)

    alert: Mapped["Alert"] = relationship(back_populates="actions")

    __table_args__ = (
        UniqueConstraint("alert_id", "sequence", name="uq_action_alert_seq"),
        CheckConstraint("sequence > 0", name="ck_action_seq"),
        CheckConstraint(
            "phase IN ('AISLAMIENTO','DESINFECCION','CLINICO','LOGISTICA')",
            name="ck_action_phase",
        ),
        CheckConstraint(
            "requires_human_confirmation IN (0,1)", name="ck_action_confirm"
        ),
        CheckConstraint(
            "verification_type IN ('QR_SCAN','PHOTO_MATCH','TACTILE') OR verification_type IS NULL",
            name="ck_action_verification",
        ),
        CheckConstraint(
            "status IN ('PENDING','EXECUTING','DONE','FAILED','SKIPPED')",
            name="ck_action_status",
        ),
    )


# --------------------------------------------------------------------------- #
# 7. Auditoría y enlace con Tierra
# --------------------------------------------------------------------------- #


class RuleExecution(Base):
    __tablename__ = "rule_execution"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[str] = mapped_column("run_at", String, nullable=False)
    rule_id: Mapped[str] = mapped_column("rule_id", String, nullable=False)
    rule_version: Mapped[str] = mapped_column("rule_version", String, nullable=False)
    matched: Mapped[int] = mapped_column("matched", Integer, nullable=False)
    severity: Mapped[str] = mapped_column("severity", String, nullable=False)
    window_start: Mapped[Optional[str]] = mapped_column("window_start", String)
    window_end: Mapped[Optional[str]] = mapped_column("window_end", String)
    latency_ms: Mapped[Optional[float]] = mapped_column("latency_ms", Float)
    notes: Mapped[str] = mapped_column("notes", Text, nullable=False, default="")

    __table_args__ = (CheckConstraint("matched IN (0,1)", name="ck_rule_matched"),)


class DownlinkPacket(Base):
    __tablename__ = "downlink_packet"

    id: Mapped[int] = mapped_column("id", Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[str] = mapped_column("created_at", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    window_from: Mapped[Optional[str]] = mapped_column("window_from", String)
    window_to: Mapped[Optional[str]] = mapped_column("window_to", String)
    payload_json: Mapped[str] = mapped_column("payload_json", Text, nullable=False)
    bytes: Mapped[int] = mapped_column("bytes", Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column("status", String, nullable=False, default="QUEUED")
    sent_at: Mapped[Optional[str]] = mapped_column("sent_at", String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','SENT','ACKED','FAILED')", name="ck_downlink_status"
        ),
    )


__all__ = [
    "AirMicrobioSample",
    "Alert",
    "AlertEvidence",
    "AutonomousAction",
    "Base",
    "BiomarkerResult",
    "CrewMember",
    "DownlinkPacket",
    "HabitatModule",
    "KpiThreshold",
    "LabSample",
    "Mission",
    "RuleExecution",
    "Sensor",
    "SensorReading",
    "SurfaceSwab",
    "SymptomReport",
    "WaterQualitySample",
]
