"""
eclss.reporting — Presentación de resultados: consola, Markdown y CSV.

Este módulo implementa en texto la jerarquía visual exigida para la tablet de
a bordo (Ingeniería de Factores Humanos de Alta Confiabilidad):

    QUÉ PASA          ≤ 6 palabras, alta prioridad
    POR QUÉ IMPORTA   1 línea de riesgo clínico
    QUÉ HACER AHORA   lista de verificación secuencial

Y respeta la decisión de diseño más importante de la sección 4: **lo que no se
muestra también es diseño**. Las alarmas derivadas no aparecen en el nivel
superior; viven archivadas bajo su causa raíz y sólo se enumeran como recuento.
La tripulación necesita tres bloques, no treinta y ocho.

El mismo contenido se emite en Markdown (informe descargable / downlink) y en
CSV (análisis posterior en Tierra).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .db import Repository
from .rules_engine import CorrelationEngine, EngineRun, RuleOutcome
from .config import Severity

LINE = "=" * 78
THIN = "-" * 78

#: Etiqueta textual redundante con el color (seguridad para daltonismo).
#: En consola y en la tablet, el color nunca va solo: el texto y el icono
#: geométrico dicen lo mismo que el semáforo.
GLYPH = {
    Severity.CRITICAL: ">>> CRITICAL <<<",
    Severity.WARNING: ">>  WARNING  <<",
    Severity.OK: "    ok        ",
}


# --------------------------------------------------------------------------- #
# Utilidades de formato
# --------------------------------------------------------------------------- #


def _fmt(value: float | None, unit: str = "") -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        text = f"{value:,.0f}"
    elif abs(value) >= 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}"
    return f"{text} {unit}".strip()


def _sorted_visible(run: EngineRun) -> list[RuleOutcome]:
    """Orden de atención: crítico antes que advertencia, luego por score."""
    return sorted(
        run.visible,
        key=lambda o: (-o.severity.rank, -o.risk_score, o.rule_id),
    )


def _action_lines(outcome: RuleOutcome) -> list[str]:
    lines: list[str] = []
    for action in outcome.actions:
        confirm = "requiere confirmación" if action.requires_human_confirmation else "automática"
        extra = []
        if action.qr_code:
            extra.append(f"QR {action.qr_code}")
        if action.storage_location:
            extra.append(action.storage_location)
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(
            f"      {action.sequence}. [{action.phase}] {action.description}"
            f"\n         → {action.action_type} · {confirm}{suffix}"
        )
    return lines


# --------------------------------------------------------------------------- #
# Vista de consola (triaje)
# --------------------------------------------------------------------------- #


def render_console(run: EngineRun, repo: Repository) -> str:
    """Informe de triaje para la terminal: lo mismo que vería la tablet."""
    mission = repo.conn.execute(
        "SELECT name, comm_latency_min FROM mission ORDER BY id LIMIT 1"
    ).fetchone()
    out: list[str] = []

    out.append(LINE)
    out.append("  MÓDULO ENTORNOS CERRADOS HOSTILES — TRIAJE AUTÓNOMO")
    out.append(f"  {mission['name'] if mission else 'misión sin nombre'}")
    out.append(
        f"  VENTANA {run.window_start.isoformat()} → {run.window_end.isoformat()}"
    )
    out.append(
        f"  LATENCIA TIERRA: {mission['comm_latency_min'] if mission else 24} MIN "
        f"· MODO AUTÓNOMO ACTIVO · MOTOR v{CorrelationEngine.RULE_VERSION}"
    )
    out.append(LINE)

    visible = _sorted_visible(run)
    critical = [o for o in visible if o.severity is Severity.CRITICAL]
    warning = [o for o in visible if o.severity is Severity.WARNING]

    out.append(
        f"  ALERTAS VISIBLES: {len(visible)} "
        f"({len(critical)} críticas, {len(warning)} advertencias)   "
        f"|  SUPRIMIDAS POR CORRELACIÓN: {len(run.suppressed)}"
    )
    out.append("")

    for outcome in visible:
        tag = GLYPH.get(outcome.severity, "")
        out.append(f"  {tag}  {outcome.rule_id}   (score {outcome.risk_score:g}/100)")
        out.append(f"  QUÉ PASA        : {outcome.title}")
        out.append(f"  POR QUÉ IMPORTA : {outcome.clinical_impact}")
        if outcome.criteria:
            out.append("  CRITERIOS       :")
            for criterion in outcome.criteria:
                mark = "cumple" if criterion.satisfied else "NO cumple"
                out.append(f"      [{mark}] {criterion.name} — {criterion.detail}")
        if outcome.actions:
            out.append("  QUÉ HACER AHORA :")
            out.extend(_action_lines(outcome))
        top_evidence = sorted(outcome.evidence, key=lambda e: -e.contribution)[:4]
        if top_evidence:
            out.append("  EVIDENCIA       :")
            for ev in top_evidence:
                out.append(
                    f"      · {ev.metric}: {_fmt(ev.observed_value, ev.unit)} "
                    f"(umbral {_fmt(ev.threshold_value, ev.unit)})"
                )
        out.append(THIN)

    if run.suppressed:
        by_root: dict[str, int] = {}
        for outcome in run.suppressed:
            by_root[outcome.suppressed_by or "?"] = by_root.get(outcome.suppressed_by or "?", 0) + 1
        out.append("  ALARMAS DERIVADAS SILENCIADAS (consultables, no gritan):")
        for root, count in sorted(by_root.items()):
            out.append(f"      {count:>2} bajo {root}")
        out.append("")

    out.append("  AUDITORÍA")
    out.append(f"      reglas evaluadas      : {len(run.outcomes)}")
    out.append(f"      latencia del motor    : {run.latency_ms:.1f} ms")
    out.append(f"      alertas persistidas   : {repo.count('alert')}")
    out.append(f"      paquetes de downlink  : {repo.count('downlink_packet')}")
    out.append(LINE)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Informe Markdown (downlink a Tierra)
# --------------------------------------------------------------------------- #


def render_markdown(run: EngineRun, repo: Repository) -> str:
    """Informe completo en Markdown, apto para el paquete de transmisión."""
    mission = repo.conn.execute(
        "SELECT code, name, comm_latency_min FROM mission ORDER BY id LIMIT 1"
    ).fetchone()
    md: list[str] = []
    md.append("# Módulo Entornos Cerrados Hostiles — Informe de triaje autónomo")
    md.append("")
    md.append(f"- **Misión**: {mission['name'] if mission else '—'} (`{mission['code'] if mission else '—'}`)")
    md.append(f"- **Latencia Tierra**: {mission['comm_latency_min'] if mission else 24} min (ida y vuelta)")
    md.append("- **Modo**: autónomo (sin confirmación de Tierra)")
    md.append(f"- **Ventana analizada**: `{run.window_start.isoformat()}` → `{run.window_end.isoformat()}`")
    md.append(f"- **Motor**: `v{CorrelationEngine.RULE_VERSION}` · {len(run.outcomes)} reglas evaluadas "
              f"en {run.latency_ms:.1f} ms")
    md.append("")

    visible = _sorted_visible(run)
    md.append("## Resumen")
    md.append("")
    md.append("| Severidad | Alertas visibles | Alarmas derivadas suprimidas |")
    md.append("| --- | --- | --- |")
    md.append(
        f"| Crítica | {sum(1 for o in visible if o.severity is Severity.CRITICAL)} "
        f"| {len(run.suppressed)} |"
    )
    md.append(
        f"| Advertencia | {sum(1 for o in visible if o.severity is Severity.WARNING)} | — |"
    )
    md.append("")

    md.append("## Alertas visibles")
    md.append("")
    for outcome in visible:
        marker = "🔴" if outcome.severity is Severity.CRITICAL else "🟠"
        md.append(f"### {marker} {outcome.title}")
        md.append("")
        md.append(f"**Qué pasa.** {outcome.title}  ")
        md.append(f"**Por qué importa.** {outcome.clinical_impact}  ")
        md.append(f"**Regla.** `{outcome.rule_id}` · score {outcome.risk_score:g}/100 · "
                  f"categoría {outcome.category}")
        md.append("")
        if outcome.criteria:
            md.append("| Criterio | ¿Cumple? | Detalle |")
            md.append("| --- | --- | --- |")
            for criterion in outcome.criteria:
                md.append(
                    f"| {criterion.name} | {'sí' if criterion.satisfied else 'no'} "
                    f"| `{criterion.detail}` |"
                )
            md.append("")
        if outcome.actions:
            md.append("**Qué hacer ahora** (lista de verificación secuencial):")
            md.append("")
            md.append("| # | Fase | Acción | Verificación |")
            md.append("| --- | --- | --- | --- |")
            for action in outcome.actions:
                verify = action.verification_type or "—"
                if action.qr_code:
                    verify += f" (`{action.qr_code}`)"
                if action.storage_location:
                    verify += f" — {action.storage_location}"
                md.append(
                    f"| {action.sequence} | {action.phase} | {action.description} "
                    f"| {verify} |"
                )
            md.append("")
        top = sorted(outcome.evidence, key=lambda e: -e.contribution)[:6]
        if top:
            md.append("**Evidencia**")
            md.append("")
            md.append("| Métrica | Observado | Umbral | Unidad | Peso |")
            md.append("| --- | --- | --- | --- | --- |")
            for ev in top:
                md.append(
                    f"| `{ev.metric}` | {_num(ev.observed_value)} | {_num(ev.threshold_value)} "
                    f"| {ev.unit or '—'} | {ev.contribution:.2f} |"
                )
            md.append("")

    if run.suppressed:
        md.append("## Alarmas derivadas suprimidas")
        md.append("")
        md.append(
            "Agrupadas bajo su causa raíz para evitar la cascada de alarmas. "
            "Siguen siendo consultables y auditables; no se han borrado."
        )
        md.append("")
        md.append("| Causa raíz | Alarma derivada | Severidad | Valor |")
        md.append("| --- | --- | --- | --- |")
        for outcome in sorted(run.suppressed, key=lambda o: (o.suppressed_by or "", o.rule_id)):
            value = outcome.evidence[0].observed_value if outcome.evidence else None
            unit = outcome.evidence[0].unit if outcome.evidence else ""
            md.append(
                f"| `{outcome.suppressed_by}` | {outcome.title} | {outcome.severity.value} "
                f"| {_num(value)} {unit or ''} |"
            )
        md.append("")

    md.append("## Acciones autónomas registradas")
    md.append("")
    md.append("| Alerta | # | Fase | Acción | Estado |")
    md.append("| --- | --- | --- | --- | --- |")
    rows = repo.conn.execute(
        """
        SELECT a.rule_id, ac.sequence, ac.phase, ac.action_type, ac.status
        FROM autonomous_action ac JOIN alert a ON a.id = ac.alert_id
        ORDER BY a.rule_id, ac.sequence
        """
    ).fetchall()
    for row in rows:
        md.append(
            f"| `{row['rule_id']}` | {row['sequence']} | {row['phase']} "
            f"| `{row['action_type']}` | {row['status']} |"
        )
    md.append("")

    md.append("## Cola de transmisión a Tierra")
    md.append("")
    packets = repo.conn.execute(
        "SELECT created_at, kind, status, bytes FROM downlink_packet ORDER BY id"
    ).fetchall()
    if packets:
        md.append("| Creado | Tipo | Estado | Bytes |")
        md.append("| --- | --- | --- | --- |")
        for p in packets:
            md.append(f"| {p['created_at']} | {p['kind']} | {p['status']} | {p['bytes']:,} |")
    else:
        md.append("_Sin paquetes en cola._")
    md.append("")
    return "\n".join(md)


def _num(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- #
# Exportación CSV
# --------------------------------------------------------------------------- #

#: Tablas exportables y su orden cronológico (telemetría excluida por volumen).
CSV_TABLES: tuple[tuple[str, str], ...] = (
    ("alert", "created_at"),
    ("alert_evidence", "id"),
    ("autonomous_action", "alert_id"),
    ("air_microbio_sample", "sampled_at"),
    ("surface_swab", "sampled_at"),
    ("water_quality_sample", "sampled_at"),
    ("lab_sample", "collected_at"),
    ("biomarker_result", "measured_at"),
    ("symptom_report", "reported_at"),
    ("rule_execution", "run_at"),
    ("downlink_packet", "created_at"),
)


def export_csv(repo: Repository, out_dir: str | Path, *, include_telemetry: bool = False) -> dict[str, Path]:
    """Vuelca las tablas de hechos a CSV (una por tabla) para análisis en Tierra."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    tables = list(CSV_TABLES)
    if include_telemetry:
        tables.append(("sensor_reading", "ts_utc"))

    for table, order_by in tables:
        path = directory / f"{table}.csv"
        cursor = repo.conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
        rows = cursor.fetchall()
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([d[0] for d in cursor.description])
            for row in rows:
                writer.writerow([row[k] for k in row.keys()])
        written[table] = path
    return written


# --------------------------------------------------------------------------- #
# Punto de entrada de alto nivel
# --------------------------------------------------------------------------- #


def write_reports(
    run: EngineRun,
    repo: Repository,
    out_dir: str | Path = "out",
    *,
    include_telemetry: bool = False,
) -> dict[str, Any]:
    """
    Escribe el informe Markdown y los CSV, y devuelve las rutas generadas.

    La consola se imprime aparte (``render_console``) para que el llamador
    decida si quiere mostrarla o empaquetarla.
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)

    report_path = directory / "reporte_triaje.md"
    report_path.write_text(render_markdown(run, repo), encoding="utf-8")

    console_path = directory / "triaje.txt"
    console_path.write_text(render_console(run, repo), encoding="utf-8")

    csvs = export_csv(repo, directory / "csv", include_telemetry=include_telemetry)

    return {"markdown": report_path, "console": console_path, "csv": csvs}


__all__ = [
    "CSV_TABLES",
    "export_csv",
    "render_console",
    "render_markdown",
    "write_reports",
]
