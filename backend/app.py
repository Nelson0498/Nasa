import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from sqlalchemy.orm.attributes import flag_modified
from models import (
    db,
    LecturaAmbiental,
    MuestraMicrobiologica,
    BiomarcadorTripulante,
    AlertaAutonoma,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")

app = Flask(__name__)
CORS(app)

# Usa SQLite por defecto; soporta MySQL vía variable de entorno DATABASE_URL
# Ejemplo MySQL: export DATABASE_URL="mysql+pymysql://usuario:clave@localhost/mars_eclss"
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'mars_eclss.db')}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)


def inicializar_datos_nominales():
    """Carga una línea base nominal si la base de datos está vacía.

    Devuelve ``True`` si sembró datos y ``False`` si ya había datos (idempotente:
    arrancar varias veces no duplica filas).
    """
    if LecturaAmbiental.query.first():
        return False

    db.session.add(
        LecturaAmbiental(
            modulo="Hábitat Principal",
            presion_kpa=101.1,
            temperatura_c=21.5,
            co2_ppm=950,
            o2_porcentaje=21.0,
            voc_ppb=110,
            ruido_db=52.0,
            luz_lux=480,
            fase_circadiana="DIA",
        )
    )
    db.session.add(
        MuestraMicrobiologica(
            ubicacion="Circuito Agua Potable (WRS-A)",
            tipo="AGUA",
            bacterias_ufc=18,
            hongos_ufc=12,
            patogeno_detectado="Ninguno",
        )
    )
    db.session.add_all(
        [
            BiomarcadorTripulante(
                tripulante="Cmdt. Vega",
                cortisol_nmol=11.4,
                siga_ug_ml=135.0,
                il6_pg_ml=1.8,
                carga_viral_ebv=120,
            ),
            BiomarcadorTripulante(
                tripulante="Esp. Castillo",
                cortisol_nmol=12.1,
                siga_ug_ml=128.0,
                il6_pg_ml=2.1,
                carga_viral_ebv=95,
            ),
        ]
    )
    db.session.commit()
    return True


def ejecutar_motor_correlacion():
    """
    Cruza el último estado ambiental y microbiológico con los biomarcadores
    de la tripulación para emitir protocolos autónomos sin intervención de Tierra.
    """
    lectura = LecturaAmbiental.query.order_by(LecturaAmbiental.id.desc()).first()
    muestra = MuestraMicrobiologica.query.order_by(MuestraMicrobiologica.id.desc()).first()
    tripulantes = BiomarcadorTripulante.query.order_by(BiomarcadorTripulante.id.desc()).limit(2).all()

    if not (lectura and muestra and tripulantes):
        return []

    nuevas_alertas = []

    def crear_si_no_existe(codigo, nivel, titulo, diagnostico, pasos):
        existe = AlertaAutonoma.query.filter_by(codigo_regla=codigo, estado="ACTIVA").first()
        if not existe:
            alerta = AlertaAutonoma(
                codigo_regla=codigo,
                nivel=nivel,
                titulo=titulo,
                diagnostico=diagnostico,
                protocolo_pasos=[{"orden": i + 1, "accion": p, "completado": False} for i, p in enumerate(pasos)],
                lectura_id=lectura.id,
            )
            db.session.add(alerta)
            nuevas_alertas.append(alerta)

    # REGLA 1: Proliferación microbiana + Supresión inmune / Inflamación
    inmunodeprimidos = [t for t in tripulantes if t.siga_ug_ml < 60 or t.il6_pg_ml > 5.0]
    if (muestra.bacterias_ufc > 100 or muestra.hongos_ufc > 100) and inmunodeprimidos:
        nombres = ", ".join(t.tripulante for t in inmunodeprimidos)
        crear_si_no_existe(
            codigo="CORR_BIOFILM_INMUNE",
            nivel="CRITICA",
            titulo="Biopelícula Microbiana + Compromiso Inmunológico",
            diagnostico=(
                f"Conteo elevado en {muestra.ubicacion} ({muestra.bacterias_ufc} UFC/mL bact, "
                f"{muestra.hongos_ufc} UFC hongos) correlacionado con caída de sIgA e inflamación (IL-6) en {nombres}."
            ),
            pasos=[
                "Aislar válvula solenoide del Circuito Hídrico A y conmutar a Reserva Estéril B.",
                "Iniciar ciclo de pasteurización térmica a 85 °C durante 60 min en Circuito A.",
                "Colocar respirador N95 y descontaminar paneles adyacentes con peróxido al 0.5%.",
                "Administrar protocolo profiláctico: Zinc 30 mg + Vitamina D3 4000 UI + Probiótico LGG.",
                "Registrar paquete telemétrico en cola de transmisión diferida a Houston (24 min).",
            ],
        )

    # REGLA 2: Estrés acústico/circadiano + Reactivación viral latente (EBV)
    estres_ambiental = lectura.ruido_db > 68.0 or (lectura.fase_circadiana == "NOCHE" and lectura.luz_lux > 50)
    reactivacion_viral = [t for t in tripulantes if t.cortisol_nmol > 22.0 and t.carga_viral_ebv > 10000]
    if estres_ambiental and reactivacion_viral:
        nombres = ", ".join(t.tripulante for t in reactivacion_viral)
        crear_si_no_existe(
            codigo="CORR_CIRCADIANO_VIRAL",
            nivel="CRITICA",
            titulo="Disrupción Circadiana/Acústica + Reactivación Viral (EBV)",
            diagnostico=(
                f"Ruido ({lectura.ruido_db} dB) / Luz ({lectura.luz_lux} lux en fase {lectura.fase_circadiana}) "
                f"elevó cortisol (>22 nmol/L) y reactivó carga viral EBV (>10,000 copias/mL) en {nombres}."
            ),
            pasos=[
                "Forzar filtro espectral ámbar (<550 nm) y activar cancelación acústica en módulos de descanso.",
                "Administrar Melatonina 3 mg (1 h antes del ciclo oscuro) para resincronizar eje HPA.",
                "Extraer Valaciclovir 1000 mg del Casillero Médico B4 (escanear QR antes de administrar).",
                "Reasignar tareas extravehiculares o de alta carga cognitiva durante las próximas 48 horas.",
            ],
        )

    # REGLA 3: Hipercapnia (CO2 alto) aislada o con estrés
    if lectura.co2_ppm > 4000:
        crear_si_no_existe(
            codigo="ECLSS_CO2_ALTO",
            nivel="ADVERTENCIA",
            titulo="Hipercapnia de Cabina (CO₂ > 4000 ppm)",
            diagnostico=f"Acumulación de CO₂ ({lectura.co2_ppm} ppm) detectada en {lectura.modulo}. Riesgo de fatiga cognitiva.",
            pasos=[
                "Aumentar flujo de ventilación hacia lechos de zeolita regenerativa (CDRA) al 100%.",
                "Verificar saturación del filtro de aminas sólidas en bahía ECLSS-2.",
            ],
        )

    db.session.commit()
    return nuevas_alertas


# --- ENDPOINTS REST ---


@app.route("/")
def servir_frontend():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard():
    lectura = LecturaAmbiental.query.order_by(LecturaAmbiental.id.desc()).first()
    muestra = MuestraMicrobiologica.query.order_by(MuestraMicrobiologica.id.desc()).first()
    # Obtener última lectura por cada tripulante
    tripulantes = BiomarcadorTripulante.query.order_by(BiomarcadorTripulante.id.desc()).limit(2).all()
    alertas = AlertaAutonoma.query.order_by(AlertaAutonoma.id.desc()).limit(10).all()

    return jsonify(
        {
            "ambiental": lectura.to_dict() if lectura else None,
            "microbiologia": muestra.to_dict() if muestra else None,
            "tripulacion": [t.to_dict() for t in tripulantes],
            "alertas": [a.to_dict() for a in alertas],
        }
    )


@app.route("/api/telemetria", methods=["POST"])
def post_telemetria():
    data = request.get_json(silent=True) or {}

    if "ambiental" in data:
        amb = data["ambiental"]
        db.session.add(
            LecturaAmbiental(
                modulo=amb.get("modulo", "Hábitat Principal"),
                presion_kpa=float(amb.get("presion_kpa", 101.0)),
                temperatura_c=float(amb.get("temperatura_c", 21.5)),
                co2_ppm=int(amb.get("co2_ppm", 1000)),
                o2_porcentaje=float(amb.get("o2_porcentaje", 21.0)),
                voc_ppb=int(amb.get("voc_ppb", 120)),
                ruido_db=float(amb.get("ruido_db", 55.0)),
                luz_lux=int(amb.get("luz_lux", 450)),
                fase_circadiana=amb.get("fase_circadiana", "DIA"),
            )
        )

    if "microbiologia" in data:
        mic = data["microbiologia"]
        db.session.add(
            MuestraMicrobiologica(
                ubicacion=mic.get("ubicacion", "Circuito Agua WRS-A"),
                tipo=mic.get("tipo", "AGUA"),
                bacterias_ufc=int(mic.get("bacterias_ufc", 20)),
                hongos_ufc=int(mic.get("hongos_ufc", 15)),
                patogeno_detectado=mic.get("patogeno_detectado", "Ninguno"),
            )
        )

    if "biomarcador" in data:
        bio = data["biomarcador"]
        db.session.add(
            BiomarcadorTripulante(
                tripulante=bio.get("tripulante", "Cmdt. Vega"),
                cortisol_nmol=float(bio.get("cortisol_nmol", 12.0)),
                siga_ug_ml=float(bio.get("siga_ug_ml", 130.0)),
                il6_pg_ml=float(bio.get("il6_pg_ml", 2.0)),
                carga_viral_ebv=int(bio.get("carga_viral_ebv", 100)),
            )
        )

    db.session.commit()
    alertas_generadas = ejecutar_motor_correlacion()

    return jsonify(
        {
            "status": "ok",
            "alertas_disparadas": len(alertas_generadas),
            "alertas": [a.to_dict() for a in alertas_generadas],
        }
    ), 201


@app.route("/api/simular-crisis", methods=["POST"])
def simular_crisis():
    data = request.get_json(silent=True) or {}

    # Permite volver al estado nominal con un clic desde la interfaz
    if data.get("restaurar"):
        AlertaAutonoma.query.filter_by(estado="ACTIVA").update({"estado": "RESUELTA"})
        db.session.add(
            LecturaAmbiental(
                presion_kpa=101.2,
                temperatura_c=21.2,
                co2_ppm=890,
                o2_porcentaje=21.1,
                voc_ppb=95,
                ruido_db=50.0,
                luz_lux=5,
                fase_circadiana="NOCHE",
            )
        )
        db.session.add(
            MuestraMicrobiologica(
                ubicacion="Circuito Agua Potable (WRS-B)",
                tipo="AGUA",
                bacterias_ufc=14,
                hongos_ufc=10,
                patogeno_detectado="Ninguno",
            )
        )
        db.session.add_all(
            [
                BiomarcadorTripulante(
                    tripulante="Cmdt. Vega", cortisol_nmol=10.5, siga_ug_ml=140.0, il6_pg_ml=1.6, carga_viral_ebv=80
                ),
                BiomarcadorTripulante(
                    tripulante="Esp. Castillo", cortisol_nmol=11.0, siga_ug_ml=132.0, il6_pg_ml=1.9, carga_viral_ebv=90
                ),
            ]
        )
        db.session.commit()
        return jsonify({"status": "nominal_restaurado"})

    # Inyecta crisis combinada: fallo de bomba (ruido + luz nocturna) + biofilm + inmunosupresión
    db.session.add(
        LecturaAmbiental(
            modulo="Hábitat Principal",
            presion_kpa=99.8,
            temperatura_c=25.4,
            co2_ppm=4350,
            o2_porcentaje=20.2,
            voc_ppb=410,
            ruido_db=76.5,
            luz_lux=240,
            fase_circadiana="NOCHE",
        )
    )
    db.session.add(
        MuestraMicrobiologica(
            ubicacion="Circuito Agua Potable (WRS-A)",
            tipo="AGUA",
            bacterias_ufc=340,
            hongos_ufc=185,
            patogeno_detectado="Pseudomonas aeruginosa / Aspergillus",
        )
    )
    db.session.add_all(
        [
            BiomarcadorTripulante(
                tripulante="Cmdt. Vega",
                cortisol_nmol=28.4,
                siga_ug_ml=42.0,
                il6_pg_ml=8.7,
                carga_viral_ebv=24500,
            ),
            BiomarcadorTripulante(
                tripulante="Esp. Castillo",
                cortisol_nmol=24.1,
                siga_ug_ml=55.0,
                il6_pg_ml=6.4,
                carga_viral_ebv=14200,
            ),
        ]
    )
    db.session.commit()

    alertas = ejecutar_motor_correlacion()
    return jsonify({"status": "crisis_simulada", "alertas": [a.to_dict() for a in alertas]}), 201


@app.route("/api/alertas/<int:alerta_id>/paso/<int:orden>", methods=["POST"])
def toggle_paso_alerta(alerta_id, orden):
    """Marca/desmarca un paso del checklist y resuelve la alerta si todos están listos."""
    alerta = db.get_or_404(AlertaAutonoma, alerta_id)
    pasos = list(alerta.protocolo_pasos)

    for paso in pasos:
        if paso["orden"] == orden:
            paso["completado"] = not paso["completado"]

    alerta.protocolo_pasos = pasos
    flag_modified(alerta, "protocolo_pasos")

    if all(p["completado"] for p in pasos):
        alerta.estado = "RESUELTA"
    else:
        alerta.estado = "ACTIVA"

    db.session.commit()
    return jsonify(alerta.to_dict())


if __name__ == "__main__":
    with app.app_context():
        db.create_all()                     # crea las tablas si no existen
        sembrado = inicializar_datos_nominales()  # siembra sólo si están vacías
    print(
        "ECLSS · base lista"
        + (" (datos nominales precargados)" if sembrado else " (datos existentes conservados)")
        + " — http://127.0.0.1:5000"
    )
    app.run(host="127.0.0.1", port=5000, debug=True)
