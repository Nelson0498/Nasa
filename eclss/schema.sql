-- ===========================================================================
--  eclss/schema.sql
--  Esquema relacional canónico del módulo «Entornos Cerrados Hostiles».
--  Dialecto: SQLite (MVP local). Las anotaciones -- [PG] indican el tipo
--  equivalente en PostgreSQL para la migración a bordo / centro de control.
--
--  Organización
--    1. Referencia (misión, módulos, tripulación, sensores, umbrales)
--    2. Telemetría ECLSS continua          (alta frecuencia, sólo INSERT)
--    3. Microbiología                      (muestreo semanal / dirigido)
--    4. Laboratorio clínico de la tripulación
--    5. Síntomas reportados por la tripulación
--    6. Alertas, evidencia y acciones autónomas
--    7. Auditoría y enlace con Tierra
--
--  Convenciones
--    * Marcas de tiempo: TEXT ISO-8601 UTC con sufijo Z
--      ('2025-03-01T14:32:00Z'). Ordenable lexicográficamente y portable.
--    * Booleanos: INTEGER 0/1 con CHECK.
--    * JSON embebido: TEXT con JSON válido (SQLite JSON1 disponible).
--    * Toda FK declara ON DELETE; las tablas de hechos son inmutables.
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. REFERENCIA
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mission (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    code              TEXT    NOT NULL UNIQUE,          -- 'MARS-3Y-01'
    name              TEXT    NOT NULL,
    launch_utc        TEXT,                             -- ISO-8601
    comm_latency_min  INTEGER NOT NULL DEFAULT 24        -- ida y vuelta
                      CHECK (comm_latency_min >= 0),
    habitat_timezone  TEXT    NOT NULL DEFAULT 'UTC',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS habitat_module (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  INTEGER NOT NULL REFERENCES mission(id) ON DELETE CASCADE,
    code        TEXT    NOT NULL UNIQUE,                 -- 'HAB-01'
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL                          -- HAB | LAB | NODE | AIRLOCK
                CHECK (kind IN ('HAB','LAB','NODE','AIRLOCK','GREENHOUSE')),
    volume_m3   REAL    NOT NULL CHECK (volume_m3 > 0)
);
CREATE INDEX IF NOT EXISTS ix_module_mission ON habitat_module(mission_id);

CREATE TABLE IF NOT EXISTS crew_member (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id                INTEGER NOT NULL REFERENCES mission(id) ON DELETE CASCADE,
    code                      TEXT    NOT NULL UNIQUE,   -- 'CDR-01'
    role                      TEXT    NOT NULL,
    sex                       TEXT    CHECK (sex IN ('F','M','X')),
    birth_year                INTEGER,
    -- Línea base pre-vuelo: imprescindible para umbrales relativos (sIgA, cortisol)
    baseline_siga_ug_ml       REAL,
    baseline_cortisol_am      REAL,                       -- nmol/L
    baseline_cortisol_night   REAL,                       -- nmol/L
    terrestrial_allergy_hx    INTEGER NOT NULL DEFAULT 0
                              CHECK (terrestrial_allergy_hx IN (0,1)),
    active                    INTEGER NOT NULL DEFAULT 1
                              CHECK (active IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_crew_mission ON crew_member(mission_id);

CREATE TABLE IF NOT EXISTS sensor (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id      INTEGER NOT NULL REFERENCES habitat_module(id) ON DELETE CASCADE,
    code           TEXT    NOT NULL UNIQUE,              -- 'SEN-HAB-01-PPCO2'
    kind           TEXT    NOT NULL,                     -- ver config.SensorKind
    unit           TEXT    NOT NULL,
    location_label TEXT,
    sample_hz      REAL    NOT NULL CHECK (sample_hz > 0),
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);
CREATE INDEX IF NOT EXISTS ix_sensor_module ON sensor(module_id);

-- Fotografía versionada del catálogo de umbrales (trazabilidad médica):
-- cada alerta referencia el `threshold_version` con el que fue evaluada.
CREATE TABLE IF NOT EXISTS kpi_threshold (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kpi_key       TEXT    NOT NULL,
    version       INTEGER NOT NULL,
    unit          TEXT    NOT NULL,
    direction     TEXT    NOT NULL,
    nominal_low   REAL,
    nominal_high  REAL,
    warn_low      REAL,
    warn_high     REAL,
    crit_low      REAL,
    crit_high     REAL,
    source        TEXT    NOT NULL DEFAULT 'HRP',
    note          TEXT    NOT NULL DEFAULT '',
    UNIQUE (kpi_key, version)
);

-- ---------------------------------------------------------------------------
-- 2. TELEMETRÍA ECLSS CONTINUA
--    Tabla de hechos de mayor volumen. En vuelo se particiona por día y se
--    agrega a 1 min; el MVP guarda la cadencia efectiva del generador.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sensor_reading (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id   INTEGER NOT NULL REFERENCES sensor(id) ON DELETE CASCADE,
    ts_utc      TEXT    NOT NULL,                        -- ISO-8601 Z
    value       REAL    NOT NULL,
    quality     TEXT    NOT NULL DEFAULT 'OK'
                CHECK (quality IN ('OK','DOUBTFUL','INVALID','CALIBRATING')),
    ingested_at TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Idempotencia del ingest: una lectura por sensor y marca de tiempo.
    UNIQUE (sensor_id, ts_utc)
);
CREATE INDEX IF NOT EXISTS ix_reading_sensor_ts ON sensor_reading(sensor_id, ts_utc);
CREATE INDEX IF NOT EXISTS ix_reading_ts        ON sensor_reading(ts_utc);

-- ---------------------------------------------------------------------------
-- 3. MICROBIOLOGÍA (hisopados, aire, agua reciclada)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS air_microbio_sample (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id       INTEGER NOT NULL REFERENCES habitat_module(id) ON DELETE CASCADE,
    sampled_at      TEXT    NOT NULL,
    bacteria_cfu_m3 REAL    NOT NULL CHECK (bacteria_cfu_m3 >= 0),
    fungi_cfu_m3    REAL    NOT NULL CHECK (fungi_cfu_m3 >= 0),
    -- Géneros identificados, JSON: '["ASPERGILLUS","PENICILLIUM"]'
    dominant_fungi  TEXT    NOT NULL DEFAULT '[]',
    -- Compuestos orgánicos volátiles microbianos
    mvocs_ppb       REAL,
    method          TEXT    NOT NULL DEFAULT 'IMPACTOR_6STAGE',
    sampled_by      TEXT
);
CREATE INDEX IF NOT EXISTS ix_airmod_ts ON air_microbio_sample(module_id, sampled_at);

CREATE TABLE IF NOT EXISTS surface_swab (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id             INTEGER NOT NULL REFERENCES habitat_module(id) ON DELETE CASCADE,
    sampled_at            TEXT    NOT NULL,
    surface_label         TEXT    NOT NULL,              -- 'GALLEY_COUNTER'
    area_cm2              REAL    NOT NULL DEFAULT 100.0 CHECK (area_cm2 > 0),
    -- Se normaliza siempre a UFC/100 cm² para comparar con el umbral
    total_cfu_per_100cm2  REAL    NOT NULL CHECK (total_cfu_per_100cm2 >= 0),
    staphylococcus_aureus INTEGER NOT NULL DEFAULT 0
                          CHECK (staphylococcus_aureus IN (0,1)),
    mrsa                  INTEGER NOT NULL DEFAULT 0 CHECK (mrsa IN (0,1)),
    pseudomonas_aeruginosa INTEGER NOT NULL DEFAULT 0
                          CHECK (pseudomonas_aeruginosa IN (0,1)),
    -- Patógenos detectados por secuenciación portátil (MinION), JSON.
    minion_pathogens      TEXT    NOT NULL DEFAULT '[]',
    atp_rlu               REAL,                          -- ATP: biolumen residual
    sampled_by            TEXT
);
CREATE INDEX IF NOT EXISTS ix_swab_ts ON surface_swab(sampled_at);

CREATE TABLE IF NOT EXISTS water_quality_sample (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id              INTEGER NOT NULL REFERENCES habitat_module(id) ON DELETE CASCADE,
    circuit                TEXT    NOT NULL CHECK (circuit IN ('A','B')),
    sampled_at             TEXT    NOT NULL,
    toc_mg_l               REAL    NOT NULL CHECK (toc_mg_l >= 0),
    total_bacteria_cfu_ml  REAL    NOT NULL CHECK (total_bacteria_cfu_ml >= 0),
    coliforms_present      INTEGER NOT NULL DEFAULT 0
                           CHECK (coliforms_present IN (0,1)),
    silver_ppb             REAL,                         -- biocida iónico
    turbidity_ntu          REAL,
    atp_rlu                REAL
);
CREATE INDEX IF NOT EXISTS ix_water_ts ON water_quality_sample(sampled_at);

-- ---------------------------------------------------------------------------
-- 4. LABORATORIO CLÍNICO DE LA TRIPULACIÓN
--    Muestras (una fila) + resultados por analito (muchas filas).
--    Modelo EAV: los paneles cambian y los analitos son heterogéneos.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lab_sample (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    crew_member_id INTEGER NOT NULL REFERENCES crew_member(id) ON DELETE CASCADE,
    sample_type    TEXT    NOT NULL,                     -- ver config.SampleType
    panel          TEXT    NOT NULL,                     -- 'DIURNAL_CORTISOL'
    collected_at   TEXT    NOT NULL,
    device_id      TEXT    NOT NULL,                     -- 'MINI-PCR-01'
    status         TEXT    NOT NULL DEFAULT 'ANALYZED'
                   CHECK (status IN ('COLLECTED','ANALYZED','VOID','REPEAT')),
    notes          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sample_crew_ts ON lab_sample(crew_member_id, collected_at);

CREATE TABLE IF NOT EXISTS biomarker_result (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lab_sample_id  INTEGER NOT NULL REFERENCES lab_sample(id) ON DELETE CASCADE,
    crew_member_id INTEGER NOT NULL REFERENCES crew_member(id) ON DELETE CASCADE,
    analyte        TEXT    NOT NULL,                     -- ver config.Analyte
    value          REAL    NOT NULL,
    unit           TEXT    NOT NULL,
    -- Límite de detección: un valor por debajo no es cero, es "< LOD".
    lod            REAL,
    measured_at    TEXT    NOT NULL,
    flagged        INTEGER NOT NULL DEFAULT 0 CHECK (flagged IN (0,1)),
    UNIQUE (lab_sample_id, analyte)
);
CREATE INDEX IF NOT EXISTS ix_bio_crew_analyte ON biomarker_result(crew_member_id, analyte, measured_at);

-- ---------------------------------------------------------------------------
-- 5. SÍNTOMAS (entrada manual / asistente de voz de la tablet)
--    Necesarios para condiciones clínicas que no son analíticas, p. ej.
--    «SpO2 < 94 % con sibilancias» del Escenario C.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS symptom_report (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    crew_member_id INTEGER NOT NULL REFERENCES crew_member(id) ON DELETE CASCADE,
    reported_at    TEXT    NOT NULL,
    symptom_code   TEXT    NOT NULL,                     -- 'WHEEZE','RHINORRHEA'
    severity       INTEGER NOT NULL DEFAULT 1 CHECK (severity BETWEEN 1 AND 3),
    source         TEXT    NOT NULL DEFAULT 'SELF_REPORT',
    notes          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_symptom_crew_ts ON symptom_report(crew_member_id, reported_at);

-- ---------------------------------------------------------------------------
-- 6. ALERTAS, EVIDENCIA Y ACCIONES AUTÓNOMAS
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alert (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id           INTEGER NOT NULL REFERENCES mission(id) ON DELETE CASCADE,
    created_at           TEXT    NOT NULL,
    rule_id              TEXT    NOT NULL,               -- 'SCENARIO_A_WATER_IMMUNE'
    rule_version         TEXT    NOT NULL,
    route                TEXT,                           -- R1 | R2 | R3 (nullable)
    scenario             TEXT,                           -- A | B | C (nullable)
    severity             TEXT    NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
    category             TEXT    NOT NULL
                         CHECK (category IN ('ENV','MICRO','IMMUNO','INTEGRATED','SYSTEM')),
    -- UI «Regla de los 3 segundos»: máx. 6 palabras / 1 línea.
    title                TEXT    NOT NULL,
    clinical_impact      TEXT    NOT NULL,
    status               TEXT    NOT NULL DEFAULT 'OPEN'
                         CHECK (status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS','RESOLVED','SUPPRESSED')),
    -- Clave de idempotencia: evita re-emitir la misma alerta en cada corrida.
    dedup_key            TEXT    NOT NULL,
    -- Supresión en cascada: alertas derivadas apuntan a su causa raíz.
    root_cause_alert_id  INTEGER REFERENCES alert(id) ON DELETE SET NULL,
    suppressed_count     INTEGER NOT NULL DEFAULT 0,
    risk_score           REAL    NOT NULL DEFAULT 0
                         CHECK (risk_score >= 0 AND risk_score <= 100),
    threshold_version    INTEGER NOT NULL,
    window_start         TEXT,
    window_end           TEXT,
    UNIQUE (dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_alert_created ON alert(created_at);
CREATE INDEX IF NOT EXISTS ix_alert_status  ON alert(status, severity);

-- Cada alerta es auditable: qué dato concreto, en qué tabla, contra qué corte.
CREATE TABLE IF NOT EXISTS alert_evidence (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id       INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
    kind           TEXT    NOT NULL CHECK (kind IN ('ENV','MICRO','BIOMARKER','SYMPTOM')),
    ref_table      TEXT    NOT NULL,                     -- 'sensor_reading'
    ref_id         INTEGER,                              -- PK del registro citado
    metric         TEXT    NOT NULL,                     -- 'PPCO2'
    observed_value REAL,
    threshold_value REAL,
    unit           TEXT,
    contribution   REAL    NOT NULL DEFAULT 0            -- peso 0..1 en el score
);
CREATE INDEX IF NOT EXISTS ix_evidence_alert ON alert_evidence(alert_id);

-- Acciones del protocolo de autonomía. `sequence` impone el orden de la
-- lista de verificación interactiva (una instrucción por pantalla).
CREATE TABLE IF NOT EXISTS autonomous_action (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id                   INTEGER NOT NULL REFERENCES alert(id) ON DELETE CASCADE,
    sequence                   INTEGER NOT NULL CHECK (sequence > 0),
    phase                      TEXT    NOT NULL
                               CHECK (phase IN ('AISLAMIENTO','DESINFECCION','CLINICO','LOGISTICA')),
    action_type                TEXT    NOT NULL,         -- 'CLOSE_SOLENOID_VALVE'
    description                TEXT    NOT NULL,
    payload_json               TEXT    NOT NULL DEFAULT '{}',
    -- La IA no puede ejecutar acciones físicas: exigen confirmación humana.
    requires_human_confirmation INTEGER NOT NULL DEFAULT 1
                               CHECK (requires_human_confirmation IN (0,1)),
    -- Doble verificación: QR del blíster / foto del componente resaltado.
    verification_type          TEXT    CHECK (verification_type IN ('QR_SCAN','PHOTO_MATCH','TACTILE')),
    qr_code                    TEXT,
    storage_location           TEXT,                     -- 'Cajón 2, Celda B4'
    status                     TEXT    NOT NULL DEFAULT 'PENDING'
                               CHECK (status IN ('PENDING','EXECUTING','DONE','FAILED','SKIPPED')),
    executed_at                TEXT,
    executed_by                TEXT,
    UNIQUE (alert_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_action_alert ON autonomous_action(alert_id, sequence);

-- ---------------------------------------------------------------------------
-- 7. AUDITORÍA Y ENLACE CON TIERRA
-- ---------------------------------------------------------------------------

-- Traza de cada evaluación del motor (incluidas las que no disparan nada):
-- sin esto no se puede reconstruir una decisión clínica meses después.
CREATE TABLE IF NOT EXISTS rule_execution (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT    NOT NULL,
    rule_id      TEXT    NOT NULL,
    rule_version TEXT    NOT NULL,
    matched      INTEGER NOT NULL CHECK (matched IN (0,1)),
    severity     TEXT    NOT NULL,
    window_start TEXT,
    window_end   TEXT,
    latency_ms   REAL,
    notes        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_rule_exec_run ON rule_execution(run_at);
CREATE INDEX IF NOT EXISTS ix_rule_exec_id  ON rule_execution(rule_id, matched);

-- Paquete de telemetría empaquetado para transmisión diferida (latencia 24 min).
CREATE TABLE IF NOT EXISTS downlink_packet (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    kind        TEXT    NOT NULL,                        -- 'ALERT_BUNDLE','TELEMETRY_SLICE'
    window_from TEXT,
    window_to   TEXT,
    payload_json TEXT   NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'QUEUED'
                CHECK (status IN ('QUEUED','SENT','ACKED','FAILED')),
    sent_at     TEXT
);
