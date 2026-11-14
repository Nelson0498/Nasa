"""
tests.test_schema_parity — el DDL y los modelos ORM no pueden divergir.

Estrategia
----------
``schema.sql`` es la fuente canónica y ``eclss/models.py`` es su espejo
SQLAlchemy. Mantener ambos a mano garantiza que algún día se separen, y ese día
el fallo aparece en producción como «column not found».

Este test compara los dos artefactos **sin necesitar SQLAlchemy**: lee el DDL
ejecutándolo en una base en memoria (``PRAGMA table_info``) y extrae los modelos
por análisis sintáctico (AST) del archivo Python. Como todas las columnas
declaran su nombre físico como primer argumento de ``mapped_column``, el nombre
es visible estáticamente.

Si SQLAlchemy está instalado, además se comprueba que los modelos importan y
declaran las mismas tablas que el metadata.
"""

from __future__ import annotations

import ast
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "eclss" / "schema.sql"
MODELS_PATH = ROOT / "eclss" / "models.py"


def schema_tables() -> dict[str, list[str]]:
    """Tablas y columnas tal como las crea el DDL (fuente canónica)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        ]
        tables: dict[str, list[str]] = {}
        for name in names:
            if name.startswith("sqlite_"):  # sqlite_sequence (AUTOINCREMENT)
                continue
            columns = [
                row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()
            ]
            tables[name] = columns
        return tables
    finally:
        conn.close()


def model_tables() -> dict[str, list[str]]:
    """Tablas y columnas declaradas en los modelos ORM (vía AST)."""
    tree = ast.parse(MODELS_PATH.read_text(encoding="utf-8"))
    tables: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        table = None
        columns: list[str] = []
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__tablename__"
                        and isinstance(statement.value, ast.Constant)
                    ):
                        table = str(statement.value.value)
            elif isinstance(statement, ast.AnnAssign):
                value = statement.value
                # Sólo cuentan las columnas con nombre físico explícito.
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "mapped_column"
                    and value.args
                    and isinstance(value.args[0], ast.Constant)
                ):
                    columns.append(str(value.args[0].value))
        if table:
            tables[table] = columns
    return tables


class SchemaParityTest(unittest.TestCase):
    def test_both_artifacts_exist(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), "falta eclss/schema.sql")
        self.assertTrue(MODELS_PATH.is_file(), "falta eclss/models.py")

    def test_same_tables(self) -> None:
        ddl = set(schema_tables())
        orm = set(model_tables())
        self.assertEqual(
            ddl - orm, set(), "tablas del DDL que faltan en los modelos ORM"
        )
        self.assertEqual(
            orm - ddl, set(), "tablas de los modelos ORM que no existen en el DDL"
        )

    def test_same_columns(self) -> None:
        ddl = schema_tables()
        orm = model_tables()
        for table in sorted(set(ddl) & set(orm)):
            with self.subTest(table=table):
                self.assertEqual(
                    sorted(ddl[table]),
                    sorted(orm[table]),
                    f"columnas divergentes en «{table}»",
                )

    def test_models_import_when_sqlalchemy_available(self) -> None:
        """Si el entorno trae SQLAlchemy, los modelos deben importarse de verdad."""
        try:
            import sqlalchemy  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("SQLAlchemy no está instalado (dependencia opcional)")
        from eclss.models import Base

        self.assertEqual(
            set(Base.metadata.tables), set(schema_tables()),
            "los modelos importados no coinciden en tablas con el DDL",
        )


if __name__ == "__main__":
    unittest.main()
