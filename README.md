# Módulo «Entornos Cerrados Hostiles» — MVP

> **Este repositorio contiene dos entregas del mismo módulo:**
>
> | Entrega | Qué es | Dónde |
> | --- | --- | --- |
> | **Offline** (este README) | Motor de análisis en lote: esquema relacional, motor de correlación de 52 reglas y generador de 96 h. Consola + Markdown. | `eclss/`, `run_mvp.py` |
> | **Web** (3 archivos) | API REST Flask + motor de correlación + dashboard Vue 3/Tailwind. | `backend/models.py`, `backend/app.py`, `frontend/index.html` |
>
> Difieren en el propósito: la entrega offline analiza en lote; la web opera.

---

## Versión web (3 archivos)

La versión web reduce el módulo a lo esencial: **4 tablas** (el checklist vive
como columna `JSON` de `alerta_autonoma`, sin tabla extra), el **motor de
correlación** y los **endpoints REST** en `app.py`, y el **dashboard** en un
único `index.html` con Vue 3 + Tailwind por CDN. Un solo proceso Flask sirve la
API y el frontend.

```bash
pip install flask flask-sqlalchemy flask-cors pymysql
python backend/app.py          # http://127.0.0.1:5000
```

| Método | Ruta | Función |
| --- | --- | --- |
| `POST` | `/api/telemetria` | Ingesta de telemetría y evaluación del motor |
| `GET`  | `/api/dashboard` | Instantánea para el panel |
| `POST` | `/api/simular-crisis` | Crisis sintética; `{"restaurar": true}` vuelve a nominal |
| `POST` | `/api/alertas/<id>/paso/<orden>` | Marca/desmarca un paso del checklist |

SQLite por defecto (`backend/mars_eclss.db`); MySQL con
`DATABASE_URL=mysql+pymysql://usuario:clave@host/mars_eclss`.

---

Estructura técnica inicial del módulo de telemetría ECLSS + salud de la
tripulación para **Operaciones Médicas Independientes de la Tierra**
(misión a Marte de 3 años, latencia de ida y vuelta 24–48 min).

El MVP responde a tres preguntas de ingeniería, en este orden:

1. **¿Dónde vive el dato?** Un esquema relacional que separa hechos inmutables
   (telemetría, muestras) de conclusiones auditables (alertas, evidencia,
   acciones) y que versiona los umbrales con los que se decidió.
2. **¿Cómo se cruza el ambiente con la biología?** Un motor de reglas en tres
   capas (atómicas → rutas fisiopatológicas → escenarios de autonomía) que
   explica *por qué* dispara, no sólo *que* dispara.
3. **¿Con qué se prueba sin volar a Marte?** Un generador determinista de 96 h
   con una avería que progresa desde el filtro saturado hasta la reactivación
   viral, calibrado para que las tres rutas y los tres escenarios se disparen.

---

## Inicio rápido

```bash
python run_mvp.py                # ciclo completo (~2 s) e informe de triaje
python run_mvp.py --reset        # borra la base y regenera desde cero
python run_mvp.py --hours 24     # sólo el día nominal: 0 alertas (control de falsos positivos)
python -m unittest discover -s tests -t .    # 37 tests
```

Requisitos: **Python 3.11+** y nada más. El núcleo usa sólo la biblioteca
estándar. `requirements.txt` únicamente fija `SQLAlchemy`, que es opcional y
sólo hace falta para `eclss/models.py` (modelos ORM) y para la prueba de paridad
del esquema.

Salida esperada de una ejecución por defecto:

```
ALERTAS VISIBLES: 13 (13 críticas) | SUPRIMIDAS POR CORRELACIÓN: 27
Escenarios de autonomía disparados: A, B, C
52 reglas evaluadas en ~80 ms · 86 400 filas de telemetría
```

---

## Estructura

```
.
├── run_mvp.py                  Orquestador CLI (5 pasos, un comando)
├── requirements.txt            SQLAlchemy (opcional)
├── eclss/
│   ├── config.py               Catálogo de KPIs y umbrales — fuente única de verdad
│   ├── schema.sql              Esquema relacional canónico (17 tablas)
│   ├── db.py                   Repositorio SQLite (todo el SQL vive aquí)
│   ├── models.py               Modelos ORM SQLAlchemy equivalentes (opcional)
│   ├── rules_engine.py         Motor de correlación y árboles de autonomía
│   ├── synthetic.py            Generador de misión con avería progresiva
│   └── reporting.py            Triaje en consola, informe Markdown y CSV
├── tests/
│   ├── test_schema_parity.py   El DDL y los modelos ORM no pueden divergir
│   └── test_rules_engine.py    Disparo correcto, cascada y ausencia de falsos positivos
└── out/                        Generado: base SQLite, informes y CSV
```

Flujo de datos — una sola dirección:

```
config.py ──► synthetic.py ──► db.py ──► rules_engine.py ──► reporting.py
   (umbrales)   (lo que pasó)  (persistencia)  (qué significa)   (qué ve la tripulación)
```

El motor **no escribe SQL** y **no conoce la UI**. Recibe un repositorio, lee
ventanas temporales y devuelve objetos; la persistencia es un paso final
explícito (`persist=False` permite testearlo sin tocar disco).

---

## 1. Esquema relacional

`eclss/schema.sql` es la fuente canónica; `eclss/models.py` es su espejo
SQLAlchemy 2.0. El test de paridad compara los dos artefactos **por análisis
estático**, de modo que la divergencia se detecta sin necesidad de instalar el
ORM.

| Grupo | Tablas | Decisión de diseño |
| --- | --- | --- |
| Referencia | `mission`, `habitat_module`, `crew_member`, `sensor`, `kpi_threshold` | La línea base pre-vuelo de sIgA y cortisol es una columna, no un cálculo: sin ella no existen umbrales relativos. |
| Telemetría | `sensor_reading` | `UNIQUE(sensor_id, ts_utc)` + `INSERT OR IGNORE`: recargar el generador no duplica historia. |
| Microbiología | `air_microbio_sample`, `surface_swab`, `water_quality_sample` | La biocarga se normaliza siempre a UFC/100 cm² para poder compararla con el corte. |
| Clínica | `lab_sample`, `biomarker_result`, `symptom_report` | Un analito por fila (EAV): los paneles cambian y los analitos son heterogéneos. |
| Decisión | `alert`, `alert_evidence`, `autonomous_action` | Toda alerta cita la tabla, el registro y el corte concretos que la originaron. |
| Auditoría | `rule_execution`, `downlink_packet` | Se registran también las reglas que **no** dispararon: sin negativos no se puede reconstruir una decisión meses después. |

Cuatro decisiones que merecen explicación:

**Umbrales versionados, no constantes.** `config.py` es la fuente única de
verdad y se materializa en `kpi_threshold` con un `version`. Cada alerta guarda
el `threshold_version` con el que fue evaluada. Cuando el HRP revise un corte,
las alertas históricas seguirán siendo explicables con el corte que las produjo
— requisito de trazabilidad médica, no un lujo.

**Marcas de tiempo como texto ISO-8601 UTC.** Formato de ancho fijo, ordenable
lexicográficamente: `ORDER BY ts_utc` y `BETWEEN` funcionan sin funciones de
fecha y el mismo texto se lee igual en SQLite y en PostgreSQL.

**Supresión en cascada con causa raíz explícita.** `alert.root_cause_alert_id`
permite archivar quince alarmas derivadas bajo una sola alerta de sistema. No se
borran: cambian de estado a `SUPPRESSED` y siguen consultables.

**La IA no ejecuta acciones físicas.** `autonomous_action` separa
`requires_human_confirmation` de `verification_type` (`QR_SCAN`, `PHOTO_MATCH`,
`TACTILE`) y guarda `qr_code` y `storage_location` («Cajón 2, Celda B4»), que es
exactamente lo que la tablet necesita para la doble verificación farmacológica.

---

## 2. Motor de reglas de correlación

`eclss/rules_engine.py` evalúa 52 reglas en tres capas. Cada capa responde a una
pregunta distinta y ninguna sustituye a la anterior:

| Capa | Pregunta | Nº | Ejemplos de `rule_id` |
| --- | --- | --- | --- |
| **Atómicas** | ¿Qué está fuera de rango? | 44 | `ENV_PPCO2_HAB-01`, `MICRO_WATER_TOC`, `BIO_SIGA_DROP` |
| **Rutas** | ¿Qué mecanismo lo explica? | 3 | `ROUTE_R1_CIRCADIAN_VIRAL`, `ROUTE_R2_HUMIDITY_ALLERGY`, `ROUTE_R3_BIOFILM_INFECTION` |
| **Escenarios** | ¿Qué hacemos sin Tierra? | 3 | `SCENARIO_A_WATER_IMMUNE`, `SCENARIO_B_CIRCADIAN_VIRAL`, `SCENARIO_C_FUNGAL_ALLERGY` |
| **Sistema** | ¿Cuál es el fallo primario? | 2 | `SYSTEM_AIR_DEGRADED`, `SYSTEM_WATER_DEGRADED` |

Las 44 atómicas se reparten en 33 ambientales (una por módulo y canal), 6
microbiológicas y 5 de biomarcadores.

### La condición del Escenario A, literal en el código

```python
criteria = [
    Criterion("Agua fuera de rango (TOC > 5 mg/L o > 100 UFC/mL o coliformes)",
              water_ok, f"TOC={toc_max} UFC/mL={bact_max} coliformes={coliforms}"),
    Criterion("Compromiso inmune (sIgA -40 % o IL-6 > 5 pg/mL en >= 2 sujetos)",
              immune_ok, f"sIgA={siga_affected} IL6={il6_affected}"),
]
matched = all(c.satisfied for c in criteria)
```

`Criterion` hace que cada alerta sea **explicable**: la tablet no dice «riesgo
alto», dice qué dos condiciones se cumplieron y con qué cifras. Es el mismo
objeto que alimenta la consola, el Markdown y (en producción) la pantalla de
triaje.

### Cuatro decisiones que evitan errores clásicos

**Un criterio multi-día no cabe en una ventana de 24 h.** «3 noches
consecutivas de ruido > 62 dBA» y «cortisol aplanado 3 días» tienen su propio
*lookback* (`noise_lookback_hours=72`, `cortisol_lookback_days=3`). Mezclar
ventana de análisis y ventana de criterio produce un árbol de decisión que nunca
dispara en órbita.

**El cortisol se evalúa por día, no promediando la ventana.** Promediar tres
días con dos normales y uno aplanado da un ratio de 2.53 — «advertencia» — justo
cuando el tripulante ya está en 1.7. Ese error de método retrasaría la Ruta 1
48 h.

**Se clasifica con p95, no con el máximo.** La especificación habla de niveles
«continuos» y «sostenidos». Clasificar con el máximo absoluto hace que un portazo
dispare una alerta crítica y entrene a la tripulación a ignorar el rojo. El
máximo se reserva para `NOISE_PEAK`, cuyo umbral es explícitamente de pico.

**La ausencia de dato nunca es «normal».** Falta la muestra de saliva → la ruta
no se cierra en verde: queda `UNKNOWN` y la UI debe mostrar «datos
insuficientes». Confundir «no medido» con «sano» es el fallo más caro posible en
medicina remota.

### Cómo añadir una regla

1. Si introduce un umbral nuevo, añadirlo a `config.KPI` (con `source="HRP"` o
   `"MVP-OPS"`); se publica solo en `kpi_threshold`.
2. Escribir un método `_rule_mi_regla(snap) -> RuleOutcome` con su `Evidence` y
   sus `Criterion`.
3. Registrarlo en `CorrelationEngine.run()`.
4. Añadir un test que verifique el disparo **y** uno que verifique el silencio en
   operación nominal.

---

## 3. Generador de datos sintéticos

`eclss/synthetic.py` — 96 h, determinista, con una sola semilla:

| Ventana | Qué ocurre | Qué dispara |
| --- | --- | --- |
| h 0–24 | Operación nominal. Línea base de la tripulación. | nada (test de falsos positivos) |
| h 24–40 | Filtros HEPA/carbón saturados: suben humedad y TVOC; el soplador roza. | `ENV_HUMIDITY_*`, `ENV_VOC_TVOC_*`, `ENV_NOISE_*` |
| h 34–48 | Lecho de CO2 agotado y deriva del driver LED (entra azul de noche). | `ENV_PPCO2_*`, `ENV_BLUE_LIGHT_NIGHT_*` |
| h 44–60 | Biopelícula en el WRS del Circuito A. | `MICRO_WATER_TOC`, `MICRO_WATER_BACTERIA` |
| h 60–96 | Respuesta de la tripulación: cae la sIgA, sube la IL-6, se aplana el cortisol, se reactivan EBV/VZV, eosinofilia y sibilancias en MS-05. | `BIO_*`, rutas y escenarios A/B/C |

**¿Por qué 96 h y no 24?** La especificación pide 24 h nominales con una
anomalía progresiva, y esas 24 h están íntegras al principio. Se añaden 72 h por
una razón dura: el Escenario B exige 3 noches consecutivas, imposible de
satisfacer en 24 h; y los Escenarios A y C necesitan que la avería haya
progresado lo suficiente para cruzar sus umbrales. Con `--hours 24` se obtiene el
día nominal completo y **ninguna alerta** — que es el resultado correcto, y el
test de falsos positivos lo verifica.

**¿Fidelidad de cadencia?** Las frecuencias declaradas (1 Hz en presión y
`ppO2`) suponen ~7,7 millones de filas en 96 h para 30 sensores. El MVP comprime
a una cadencia configurable (`--interval`, 2 min por omisión, factor 1/30). La
densidad no cambia ninguna regla, sólo la resolución — y está documentada para
que nadie confunda «no lo medimos» con «no ocurrió».

**¿Reproducibilidad?** El ruido de cada muestra se deriva de
`Random("{seed}:{timestamp}")`, no de un generador secuencial: regenerar una
ventana concreta da exactamente los mismos valores, lo que permite fijar
escenarios de prueba sin guardar la base.

---

## 4. Salidas

`python run_mvp.py` escribe:

| Archivo | Contenido |
| --- | --- |
| `out/eclss_mvp.sqlite3` | Base completa (17 tablas, 86 400 lecturas, 40 alertas) |
| `out/triaje.txt` | Triaje en texto plano — lo que ve la tablet |
| `out/reporte_triaje.md` | Informe completo para el paquete de downlink |
| `out/csv/*.csv` | 11 tablas de hechos para análisis en Tierra |

El triaje respeta la jerarquía de la sección 4 de la especificación: **QUÉ PASA**
(≤ 6 palabras) / **POR QUÉ IMPORTA** (una línea de riesgo clínico) / **QUÉ HACER
AHORA** (lista secuencial con fase, verificación y ubicación física). Las 27
alarmas derivadas no aparecen arriba: se enumeran como recuento bajo su causa
raíz.

---

## 5. Tests

```
Ran 37 tests ... OK (skipped=1)
```

- **`test_schema_parity`** — compara tablas y columnas del DDL (ejecutándolo en
  memoria) con las declaradas en los modelos ORM (por AST). Falla si alguien
  añade una columna en un solo lado.
- **`test_rules_engine`** — umbrales, duración sobre umbral, rachas de noches,
  caída relativa de sIgA; disparo de las 3 rutas y los 3 escenarios; cascada;
  idempotencia de alertas; cola de downlink; y el test más importante:
  **un periodo nominal no produce ninguna alerta crítica**.
- La prueba de importación real de SQLAlchemy se **omite** (no se salta en falso)
  si el ORM no está instalado.

---

## 6. Límites conocidos y hoja de ruta a producción

| Ahora (MVP) | Producción a bordo |
| --- | --- |
| SQLite local | PostgreSQL + TimescaleDB (`sensor_reading` como hypertable, retención por política) |
| Escritura directa por lotes | Ingesta MQTT/OPC-UA con validación de rango y `quality` (`DOUBTFUL`/`INVALID`) |
| Cadencia comprimida a 2 min | 1 Hz real: ~7,7 M filas/96 h, agregados continuos de 1 min |
| Umbrales en `config.py` | Catálogo firmado y distribuido por el centro de control, con versionado ya soportado |
| Sin autenticación ni cifrado | RBAC por rol (CDR/médico/ECLSS), auditoría de acceso a datos clínicos |
| Reglas deterministas | Mismo motor + capa de pronóstico (tendencias) *explicable*; nunca caja negra en bucle clínico |

Advertencias que conviene dejar por escrito:

- Los umbrales marcados `MVP-OPS` (TNF-α, IgE, relación IFN-γ/IL-10, TVOC) son
  **operativos, no normativos**: la especificación sólo daba una dirección, no
  una cifra. Requieren calibración con datos de vuelo antes de cualquier uso
  clínico.
- Las dosis y pautas farmacológicas (`autonomous_action`) reproducen la
  especificación funcional. El MVP **no** sustituye la validación de un cirujano
  de vuelo ni el etiquetado del medicamento.
- El MVP demuestra el *mecanismo* (correlación → alerta → acción verificable),
  no la validación clínica del mismo.

---

## 7. Trazabilidad con la especificación

| Sección de la especificación | Dónde vive |
| --- | --- |
| A. KPIs ambientales (presión, ppO₂, ppCO₂, T, HR, ruido, luz) | `config.KPI` + `_rule_env_kpi` |
| B. KPIs microbiológicos (aire, superficies, WRS) | `_rule_air_fungi`, `_rule_air_bacteria`, `_rule_surfaces`, `_rule_water` |
| C. Biomarcadores (cortisol, sIgA, carga viral, citoquinas, leucograma) | `_rule_cortisol`, `_rule_siga`, `_rule_viral`, `_rule_cytokines`, `_rule_differential` |
| Ruta 1 — circadiano → viral | `_route_r1`, `SCENARIO_B_CIRCADIAN_VIRAL` |
| Ruta 2 — humedad → alergia | `_route_r2`, `SCENARIO_C_FUNGAL_ALLERGY` |
| Ruta 3 — biopelícula → infección | `_route_r3`, `SCENARIO_A_WATER_IMMUNE` |
| Protocolo de autonomía (árboles A/B/C) | `_actions_scenario_a/b/c` + `autonomous_action` |
| UI: regla de los 3 segundos, cascada, doble verificación | `reporting.render_console` / `render_markdown` + `root_cause_alert_id` + `qr_code`/`storage_location` |
| UI: latencia y modo autónomo | Banner en `render_console` (`LATENCIA TIERRA: 24 MIN · MODO AUTÓNOMO ACTIVO`) |
