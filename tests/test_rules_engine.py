"""
tests.test_rules_engine — comportamiento del motor y del generador.

Se organiza en tres bloques:

1. **Unidades puras** (sin base de datos): clasificación de umbrales, duración
   sobre umbral, rachas de noches, caída relativa de sIgA.
2. **Escenario de avería completa**: 96 h de misión → deben dispararse las tres
   rutas fisiopatológicas y los tres escenarios de autonomía, con supresión en
   cascada y planes de acción completos.
3. **No-regresión / falsos positivos**: un periodo nominal no debe generar
   ninguna alerta crítica. Un motor que grita sin motivo se ignora, y una alarma
   ignorada mata.
"""

from __future__ import annotations

import shutil
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from eclss import config
from eclss.config import EngineConfig, Scenario, Severity
from eclss.db import Repository, connect, parse_iso, to_iso
from eclss.rules_engine import (
    CorrelationEngine,
    consecutive_days,
    hours_over,
    nights_over,
    pct_change,
    stats,
)
from eclss.synthetic import SyntheticMission

UTC = timezone.utc
T0 = datetime(2025, 3, 1, tzinfo=UTC)


def make_repo(*, hours: float, interval_minutes: int = 5) -> Repository:
    """Repositorio en memoria con misión sintética ya generada."""
    repo = Repository(connect(":memory:"))
    repo.seed()
    SyntheticMission(hours=hours, interval_minutes=interval_minutes).generate(repo)
    return repo


def close_repo(repo: Repository) -> None:
    """Cierra la conexión (evita ResourceWarning por bases sin liberar)."""
    try:
        repo.conn.close()
    except Exception:  # pragma: no cover - ya cerrada
        pass


# --------------------------------------------------------------------------- #
# 1. Unidades puras
# --------------------------------------------------------------------------- #


class ThresholdTest(unittest.TestCase):
    def test_band_kpi_classification(self) -> None:
        humidity = config.kpi("HUMIDITY")
        self.assertIs(humidity.classify(52.0), Severity.OK)
        # La banda nominal (40–60 %) y la de advertencia (<30 o >65 %) no son
        # contiguas: 38 % está fuera de lo nominal pero aún no es advertencia.
        self.assertIs(humidity.classify(38.0), Severity.OK)
        self.assertIs(humidity.classify(28.0), Severity.WARNING)
        self.assertIs(humidity.classify(80.0), Severity.CRITICAL)
        self.assertIs(humidity.classify(None), Severity.UNKNOWN)

    def test_high_bad_kpi(self) -> None:
        ppco2 = config.kpi("PPCO2")
        self.assertIs(ppco2.classify(0.20), Severity.OK)
        self.assertIs(ppco2.classify(0.30), Severity.WARNING)
        self.assertIs(ppco2.classify(0.60), Severity.CRITICAL)

    def test_low_side_kpi(self) -> None:
        spo2 = config.kpi("SPO2")
        self.assertIs(spo2.classify(98.0), Severity.OK)
        self.assertIs(spo2.classify(94.5), Severity.WARNING)
        self.assertIs(spo2.classify(92.0), Severity.CRITICAL)

    def test_worst_aggregation(self) -> None:
        self.assertIs(
            config.worst(Severity.OK, Severity.WARNING, Severity.OK),
            Severity.WARNING,
        )
        self.assertIs(config.worst(), Severity.UNKNOWN)

    def test_threshold_snapshot_is_complete(self) -> None:
        snapshot = config.threshold_snapshot()
        self.assertEqual(len(snapshot), len(config.KPI))
        self.assertEqual(
            {row["kpi_key"] for row in snapshot}, set(config.KPI)
        )


class SeriesAnalyticsTest(unittest.TestCase):
    def test_stats_percentiles(self) -> None:
        series = [(T0 + timedelta(minutes=i), float(i)) for i in range(100)]
        summary = stats(series)
        self.assertEqual(summary.n, 100)
        self.assertAlmostEqual(summary.mean or 0, 49.5, places=6)
        self.assertEqual(summary.vmax, 99.0)
        # p95 robusto: ignora el 5 % superior, que es el objetivo del diseño.
        self.assertLess(summary.p95 or 0, summary.vmax or 0)

    def test_hours_over_integrates_duration_not_samples(self) -> None:
        """
        12 h por encima del umbral con muestras cada 10 min.

        `hours_over` integra por intervalo con «muestreo y retención» del valor
        final: con 73 muestras altas (i = 0..72) se cuentan 72 intervalos. Por eso
        el valor esperado es (72 − 1) × 10 min, no 12 h exactas.
        """
        series = [
            (T0 + timedelta(minutes=10 * i), 80.0 if i < 73 else 50.0)
            for i in range(144)
        ]
        self.assertAlmostEqual(hours_over(series, 70.0), 72 * 10 / 60, places=6)

    def test_hours_over_caps_telemetry_gaps(self) -> None:
        """Un hueco de 10 h no debe contar como 10 h de exposición."""
        series = [(T0, 50.0), (T0 + timedelta(hours=10), 90.0)]
        self.assertAlmostEqual(hours_over(series, 70.0), 1.0, places=6)

    def test_nights_over_and_consecutive_days(self) -> None:
        cfg = EngineConfig()
        # Tres noches consecutivas con un pico a las 23:00 por encima del corte.
        series = []
        for day in range(3):
            base = T0 + timedelta(days=day)
            series.append((base.replace(hour=23), 65.0))
            series.append((base.replace(hour=14), 50.0))
        nights = nights_over(series, 62.0, cfg)
        self.assertEqual(len(nights), 3)
        self.assertEqual(consecutive_days(nights), 3)
        # Dos noches separadas NO son una racha.
        self.assertEqual(consecutive_days([date(2025, 3, 1), date(2025, 3, 3)]), 1)

    def test_night_window_boundaries(self) -> None:
        """La fase de sueño debe ser exactamente 22:00–06:00 (generador ≈ motor)."""
        self.assertTrue(config.is_night(datetime(2025, 3, 1, 23, 0)))
        self.assertTrue(config.is_night(datetime(2025, 3, 1, 2, 0)))
        self.assertFalse(config.is_night(datetime(2025, 3, 1, 12, 0)))
        self.assertFalse(config.is_night(datetime(2025, 3, 1, 7, 0)))

    def test_pct_change(self) -> None:
        self.assertAlmostEqual(pct_change(100.0, 55.0) or 0, 0.45, places=6)
        self.assertIsNone(pct_change(None, 10.0))
        self.assertIsNone(pct_change(0.0, 10.0))


class SerializationTest(unittest.TestCase):
    def test_iso_roundtrip(self) -> None:
        moment = datetime(2025, 3, 4, 7, 15, 42, tzinfo=UTC)
        self.assertEqual(to_iso(moment), "2025-03-04T07:15:42Z")
        self.assertEqual(parse_iso(to_iso(moment)), moment)

    def test_iso_ordering_is_lexicographic(self) -> None:
        early, late = to_iso(T0), to_iso(T0 + timedelta(hours=1))
        self.assertLess(early, late)


# --------------------------------------------------------------------------- #
# 2. Avería completa: los tres escenarios de autonomía
# --------------------------------------------------------------------------- #


class AnomalyScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = make_repo(hours=96, interval_minutes=5)
        cls.addClassCleanup(close_repo, cls.repo)
        cls.engine_run = CorrelationEngine(cls.repo).run(persist=False)

    def test_all_routes_fire(self) -> None:
        routes = self.engine_run.by_route()
        self.assertEqual(len(routes), 3)
        for route, outcome in routes.items():
            with self.subTest(route=route.value):
                self.assertTrue(outcome.matched, f"{route.value} no disparó")
                self.assertTrue(all(c.satisfied for c in outcome.criteria))

    def test_all_scenarios_fire(self) -> None:
        scenarios = self.engine_run.by_scenario()
        self.assertEqual(len(scenarios), 3)
        for scenario, outcome in scenarios.items():
            with self.subTest(scenario=scenario.value):
                self.assertTrue(outcome.matched, f"{scenario.value} no disparó")
                self.assertIs(outcome.severity, Severity.CRITICAL)

    def test_scenario_conditions_match_specification(self) -> None:
        """Cada escenario debe citar sus dos o tres condiciones originales."""
        expected = {
            Scenario.A_WATER_IMMUNE: 2,
            Scenario.B_CIRCADIAN_VIRAL: 3,
            Scenario.C_FUNGAL_ALLERGY: 3,
        }
        for scenario, count in expected.items():
            with self.subTest(scenario=scenario.value):
                outcome = self.engine_run.by_scenario()[scenario]
                self.assertEqual(len(outcome.criteria), count)

    def test_action_plans_are_sequential_and_verifiable(self) -> None:
        for scenario, outcome in self.engine_run.by_scenario().items():
            with self.subTest(scenario=scenario.value):
                self.assertTrue(outcome.actions)
                sequences = [a.sequence for a in outcome.actions]
                self.assertEqual(sequences, sorted(sequences))
                self.assertEqual(sequences[0], 1)
                # Toda acción farmacológica exige doble verificación por QR.
                for action in outcome.actions:
                    if action.phase == "CLINICO":
                        self.assertEqual(action.verification_type, "QR_SCAN")
                        self.assertTrue(action.qr_code)
                        self.assertTrue(action.storage_location)

    def test_cascade_suppression_groups_derived_alarms(self) -> None:
        suppressed = self.engine_run.suppressed
        self.assertTrue(suppressed, "la avería debería generar cascada")
        roots = {o.rule_id: o for o in self.engine_run.actionable if o.category == "SYSTEM"}
        self.assertIn("SYSTEM_AIR_DEGRADED", roots)
        self.assertIn("SYSTEM_WATER_DEGRADED", roots)
        for outcome in suppressed:
            with self.subTest(rule=outcome.rule_id):
                self.assertIn(outcome.suppressed_by, roots)
                self.assertIn(outcome.category, {"ENV", "MICRO"})

    def test_clinical_alerts_are_never_suppressed(self) -> None:
        """Ocultar un dato del paciente no es una mejora de usabilidad."""
        for outcome in self.engine_run.actionable:
            if outcome.category in {"IMMUNO", "INTEGRATED"}:
                self.assertIsNone(outcome.suppressed_by, outcome.rule_id)

    def test_visible_count_stays_human_scale(self) -> None:
        """La «regla de los 3 segundos»: la tripulación no puede ver 40 alertas."""
        self.assertLessEqual(len(self.engine_run.visible), 15)
        self.assertGreater(len(self.engine_run.suppressed), len(self.engine_run.visible))

    def test_cortisol_flattening_detected_in_affected_crew(self) -> None:
        outcome = next(o for o in self.engine_run.outcomes if o.rule_id == "BIO_CORTISOL_FLATTENING")
        self.assertTrue(outcome.matched)
        metrics = {e.metric for e in outcome.evidence}
        self.assertIn("CORTISOL_RATIO:CDR-01", metrics)

    def test_viral_reactivation_above_critical_cutoff(self) -> None:
        outcome = next(o for o in self.engine_run.outcomes if o.rule_id == "BIO_VIRAL_REACTIVATION")
        self.assertIs(outcome.severity, Severity.CRITICAL)
        observed = {e.metric: e.observed_value for e in outcome.evidence}
        self.assertGreaterEqual(
            observed.get("EBV_DNA:PLT-02", 0.0), config.VIRAL_CRIT_COPIES_ML
        )

    def test_every_actionable_outcome_carries_evidence(self) -> None:
        for outcome in self.engine_run.actionable:
            with self.subTest(rule=outcome.rule_id):
                self.assertTrue(outcome.evidence, "alerta sin evidencia citable")

    def test_window_is_the_last_24h(self) -> None:
        span = self.engine_run.window_end - self.engine_run.window_start
        self.assertAlmostEqual(span.total_seconds(), 24 * 3600, delta=600)


# --------------------------------------------------------------------------- #
# 3. Ruido de fondo: el motor no debe disparar en operación nominal
# --------------------------------------------------------------------------- #


class NominalOperationTest(unittest.TestCase):
    """Las primeras 24 h son nominales por diseño del generador."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = make_repo(hours=12, interval_minutes=5)
        cls.addClassCleanup(close_repo, cls.repo)
        cls.engine_run = CorrelationEngine(cls.repo).run(
            end=parse_iso(to_iso(T0 + timedelta(hours=12))), persist=False
        )

    def test_no_critical_alerts(self) -> None:
        criticals = [o for o in self.engine_run.actionable if o.severity is Severity.CRITICAL]
        self.assertEqual(
            criticals, [], f"falsos positivos críticos: {[o.rule_id for o in criticals]}"
        )

    def test_no_scenario_fires(self) -> None:
        for scenario, outcome in self.engine_run.by_scenario().items():
            with self.subTest(scenario=scenario.value):
                self.assertFalse(outcome.matched)

    def test_environment_stays_nominal(self) -> None:
        for outcome in self.engine_run.outcomes:
            if outcome.category == "ENV":
                self.assertNotEqual(outcome.severity, Severity.CRITICAL, outcome.rule_id)

    def test_no_cascade_without_primary_failure(self) -> None:
        self.assertFalse([o for o in self.engine_run.outcomes if o.category == "SYSTEM"])


class PersistenceTest(unittest.TestCase):
    def test_second_run_is_idempotent(self) -> None:
        """Re-evaluar no debe duplicar alertas (clave de deduplicación)."""
        repo = make_repo(hours=96, interval_minutes=10)
        self.addCleanup(close_repo, repo)
        engine = CorrelationEngine(repo)
        first = engine.run(persist=True)
        alerts_after_first = repo.count("alert")
        second = engine.run(persist=True)

        self.assertEqual(repo.count("alert"), alerts_after_first)
        self.assertEqual(
            set(second.created_alert_ids.values()),
            set(first.created_alert_ids.values()),
        )
        self.assertGreater(alerts_after_first, 0)

    def test_actions_and_evidence_persisted(self) -> None:
        repo = make_repo(hours=96, interval_minutes=10)
        self.addCleanup(close_repo, repo)
        run = CorrelationEngine(repo).run(persist=True)
        scenario_alerts = [o for o in run.actionable if o.scenario is not None]
        self.assertTrue(scenario_alerts)
        for outcome in scenario_alerts:
            alert_id = run.created_alert_ids[outcome.rule_id]
            self.assertEqual(len(repo.actions_for(alert_id)), len(outcome.actions))
            self.assertEqual(len(repo.evidence_for(alert_id)), len(outcome.evidence))

    def test_critical_scenario_queues_downlink(self) -> None:
        repo = make_repo(hours=96, interval_minutes=10)
        self.addCleanup(close_repo, repo)
        CorrelationEngine(repo).run(persist=True)
        self.assertEqual(repo.count("downlink_packet"), 3)

    def test_rule_executions_are_logged_including_negatives(self) -> None:
        repo = make_repo(hours=96, interval_minutes=10)
        self.addCleanup(close_repo, repo)
        run = CorrelationEngine(repo).run(persist=True)
        self.assertEqual(repo.count("rule_execution"), len(run.outcomes))
        negatives = repo.conn.execute(
            "SELECT COUNT(*) AS n FROM rule_execution WHERE matched = 0"
        ).fetchone()["n"]
        self.assertGreater(negatives, 0, "la auditoría debe registrar también los no-match")


class DurabilityTest(unittest.TestCase):
    """
    Lo escrito debe sobrevivir al cierre de la conexión.

    SQLite en modo WAL descarta las transacciones no confirmadas al cerrar. Sin
    este test, el repositorio podía «funcionar» en memoria y perder toda la
    telemetría al terminar el proceso: un fallo silencioso que sólo aparecería
    al abrir la base en el siguiente turno de guardia.
    """

    def test_facts_survive_reconnect(self) -> None:
        # La base de prueba vive bajo `out/` (no en el directorio temporal del
        # sistema) para no depender de las políticas de escritura del entorno.
        # En Windows, SQLite mantiene bloqueados los archivos -wal/-shm unos
        # instantes tras cerrar, así que la limpieza es best-effort.
        tmp_dir = Path("out") / f"_test_durability_{uuid4().hex[:8]}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, str(tmp_dir), True)
        path = tmp_dir / "mision.sqlite3"

        repo = Repository(connect(path))
        repo.seed()
        SyntheticMission(hours=96, interval_minutes=15).generate(repo)
        CorrelationEngine(repo).run(persist=True)

        tables = ("sensor_reading", "lab_sample", "biomarker_result",
                  "water_quality_sample", "alert", "alert_evidence",
                  "autonomous_action", "rule_execution", "downlink_packet")
        before = {table: repo.count(table) for table in tables}
        repo.conn.close()

        reopened = Repository(connect(path))
        try:
            for table, expected in before.items():
                with self.subTest(table=table):
                    self.assertGreater(expected, 0, f"{table} quedó vacía")
                    self.assertEqual(reopened.count(table), expected)
        finally:
            reopened.conn.close()


if __name__ == "__main__":
    unittest.main()
