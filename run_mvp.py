#!/usr/bin/env python3
"""
run_mvp.py — Punto de entrada único del MVP «Entornos Cerrados Hostiles».

Ejecuta el ciclo completo en un solo comando:

    1. Crea la base SQLite y aplica `eclss/schema.sql`.
    2. Siembra referencia: misión, 3 módulos, 30 sensores, 5 tripulantes y el
       catálogo versionado de umbrales.
    3. Genera la misión sintética (24 h nominales + 72 h de avería progresiva).
    4. Corre el motor de correlación sobre la última ventana de 24 h.
    5. Imprime el triaje y escribe el informe Markdown + CSV.

Ejemplos
--------
    python run_mvp.py
    python run_mvp.py --hours 24            # sólo el primer día: A y C disparan
    python run_mvp.py --reset --csv-telemetry
    python run_mvp.py --db out/otra.sqlite3 --out out/otra

No requiere dependencias externas: sólo la biblioteca estándar de Python 3.11+.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from eclss import config
from eclss.db import DEFAULT_DB_PATH, Repository, connect
from eclss.reporting import render_console, write_reports
from eclss.rules_engine import CorrelationEngine
from eclss.synthetic import SyntheticMission


def _configure_stdout() -> None:
    """
    Fuerza UTF-8 en la salida estándar.

    El informe usa «→», «·» y acentos; en una consola Windows con cp1252 la
    impresión fallaría con UnicodeEncodeError y el operador vería un traceback
    en lugar de un triaje. `errors="replace"` degrada el carácter, nunca el
    mensaje clínico.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):  # pragma: no cover - streams exóticos
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_mvp.py",
        description=(
            "MVP del módulo Entornos Cerrados Hostiles: genera telemetría "
            "sintética con una avería progresiva y evalúa el motor de "
            "correlación riesgo-síntoma."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH),
                        help="Ruta del archivo SQLite.")
    parser.add_argument("--out", default="out",
                        help="Directorio de informes y CSV.")
    parser.add_argument("--hours", type=float, default=96.0,
                        help="Horas simuladas (24 h nominales + 72 h de avería).")
    parser.add_argument("--interval", type=int, default=2,
                        help="Cadencia de telemetría en minutos.")
    parser.add_argument("--seed", type=int, default=20250301,
                        help="Semilla del generador (misma semilla = mismos datos).")
    parser.add_argument("--reset", action="store_true",
                        help="Borra la base antes de empezar.")
    parser.add_argument("--csv-telemetry", action="store_true",
                        help="Incluye sensor_reading en la exportación CSV (84k filas).")
    parser.add_argument("--no-report", action="store_true",
                        help="No escribe informe ni CSV; sólo imprime el triaje.")
    parser.add_argument("--quiet", action="store_true",
                        help="Silencia el progreso paso a paso.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = build_parser().parse_args(argv)

    def say(message: str) -> None:
        if not args.quiet:
            print(message, flush=True)

    db_path = Path(args.db)
    if args.reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        say(f"[0/5] Base anterior eliminada: {db_path}")

    started = time.perf_counter()

    # -- 1. Esquema -------------------------------------------------------
    say(f"[1/5] Abriendo {db_path} y aplicando eclss/schema.sql")
    conn = connect(db_path)
    repo = Repository(conn)

    # -- 2. Referencia ----------------------------------------------------
    ids = repo.seed()
    say(
        f"[2/5] Referencia sembrada: misión #{ids['mission_id']}, "
        f"{repo.count('habitat_module')} módulos, {repo.count('sensor')} sensores, "
        f"{repo.count('crew_member')} tripulantes, {repo.count('kpi_threshold')} umbrales "
        f"(v{config.THRESHOLD_VERSION})"
    )

    # -- 3. Telemetría sintética -----------------------------------------
    say(
        f"[3/5] Generando {args.hours:g} h de telemetría a {args.interval} min "
        f"(semilla {args.seed})"
    )
    stats = SyntheticMission(
        hours=args.hours, interval_minutes=args.interval, seed=args.seed
    ).generate(repo)
    say(
        "      "
        + " · ".join(f"{k}={v:,}" if isinstance(v, int) else f"{k}={v}"
                     for k, v in stats.as_dict().items())
    )

    # -- 4. Motor de reglas ----------------------------------------------
    say("[4/5] Evaluando el motor de correlación")
    engine = CorrelationEngine(repo)
    run = engine.run(persist=True)
    scenarios = run.by_scenario()
    fired = [s.name.split("_")[0] for s, o in scenarios.items() if o.matched]
    say(
        f"      {len(run.outcomes)} reglas evaluadas en {run.latency_ms:.1f} ms · "
        f"{len(run.actionable)} alertas accionables · {len(run.suppressed)} suprimidas"
    )
    say(f"      Escenarios de autonomía disparados: {', '.join(sorted(fired)) or 'ninguno'}")

    # -- 5. Informes ------------------------------------------------------
    say("[5/5] Generando informes")
    if not args.no_report:
        paths = write_reports(run, repo, args.out,
                              include_telemetry=args.csv_telemetry)
        say(f"      {paths['markdown']} · {paths['console']} · "
            f"{len(paths['csv'])} CSV en {Path(args.out) / 'csv'}")

    print()
    print(render_console(run, repo))

    elapsed = time.perf_counter() - started
    print(f"  Ejecución completa en {elapsed:.2f} s · base: {db_path}")
    repo.conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
