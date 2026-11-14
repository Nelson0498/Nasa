"""Módulo «Entornos Cerrados Hostiles» — MVP de telemetría ECLSS y salud.

Paquete organizado en capas estrictas:

    config      → catálogo de KPIs y umbrales (fuente única de verdad)
    schema.sql  → esquema relacional canónico
    db          → repositorio SQLite (sólo este módulo escribe SQL)
    models      → modelos ORM SQLAlchemy equivalentes (opcional)
    rules_engine→ correlación ambiental + biomarcadores → alertas autónomas
    synthetic   → generador de telemetría sintética con anomalía progresiva
    reporting   → informe legible (consola, Markdown, CSV)
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
