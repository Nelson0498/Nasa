from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utc_now():
    return datetime.now(timezone.utc)


class LecturaAmbiental(db.Model):
    __tablename__ = "lectura_ambiental"

    id = db.Column(db.Integer, primary_key=True)
    modulo = db.Column(db.String(50), default="Hábitat Principal", nullable=False)
    presion_kpa = db.Column(db.Float, nullable=False)        # Nominal: 70.3 - 101.3 kPa
    temperatura_c = db.Column(db.Float, nullable=False)      # Nominal: 18.3 - 23.9 °C
    co2_ppm = db.Column(db.Integer, nullable=False)          # Nominal: < 2000 ppm (Crítico > 4000)
    o2_porcentaje = db.Column(db.Float, nullable=False)      # Nominal: 19.5 - 23.5 %
    voc_ppb = db.Column(db.Integer, nullable=False)          # Compuestos orgánicos volátiles (< 250 ppb)
    ruido_db = db.Column(db.Float, nullable=False)           # Nominal: < 60 dB (Crítico > 70 dB)
    luz_lux = db.Column(db.Integer, nullable=False)          # Día: ~500 lux | Noche: < 10 lux
    fase_circadiana = db.Column(db.String(10), default="DIA")  # DIA | NOCHE
    timestamp = db.Column(db.DateTime, default=utc_now)

    def to_dict(self):
        return {
            "id": self.id,
            "modulo": self.modulo,
            "presion_kpa": self.presion_kpa,
            "temperatura_c": self.temperatura_c,
            "co2_ppm": self.co2_ppm,
            "o2_porcentaje": self.o2_porcentaje,
            "voc_ppb": self.voc_ppb,
            "ruido_db": self.ruido_db,
            "luz_lux": self.luz_lux,
            "fase_circadiana": self.fase_circadiana,
            "timestamp": self.timestamp.isoformat(),
        }


class MuestraMicrobiologica(db.Model):
    __tablename__ = "muestra_microbiologica"

    id = db.Column(db.Integer, primary_key=True)
    ubicacion = db.Column(db.String(80), nullable=False)     # Ej: Sistema Agua WRS / Panel Cocina
    tipo = db.Column(db.String(20), nullable=False)          # AGUA | SUPERFICIE
    bacterias_ufc = db.Column(db.Integer, nullable=False)    # Nominal: < 50 UFC/mL (Crítico > 100)
    hongos_ufc = db.Column(db.Integer, nullable=False)       # Nominal: < 50 UFC/m³ (Crítico > 100)
    patogeno_detectado = db.Column(db.String(80), default="Ninguno")
    timestamp = db.Column(db.DateTime, default=utc_now)

    def to_dict(self):
        return {
            "id": self.id,
            "ubicacion": self.ubicacion,
            "tipo": self.tipo,
            "bacterias_ufc": self.bacterias_ufc,
            "hongos_ufc": self.hongos_ufc,
            "patogeno_detectado": self.patogeno_detectado,
            "timestamp": self.timestamp.isoformat(),
        }


class BiomarcadorTripulante(db.Model):
    __tablename__ = "biomarcador_tripulante"

    id = db.Column(db.Integer, primary_key=True)
    tripulante = db.Column(db.String(60), nullable=False)
    cortisol_nmol = db.Column(db.Float, nullable=False)      # Nominal: 2 - 18 nmol/L (Crítico > 22)
    siga_ug_ml = db.Column(db.Float, nullable=False)         # Nominal: > 100 µg/mL (Crítico < 60)
    il6_pg_ml = db.Column(db.Float, nullable=False)          # Citoquina inflamatoria (Crítico > 5.0)
    carga_viral_ebv = db.Column(db.Integer, nullable=False)  # Copias/mL saliva (Crítico > 10000)
    timestamp = db.Column(db.DateTime, default=utc_now)

    def to_dict(self):
        return {
            "id": self.id,
            "tripulante": self.tripulante,
            "cortisol_nmol": self.cortisol_nmol,
            "siga_ug_ml": self.siga_ug_ml,
            "il6_pg_ml": self.il6_pg_ml,
            "carga_viral_ebv": self.carga_viral_ebv,
            "timestamp": self.timestamp.isoformat(),
        }


class AlertaAutonoma(db.Model):
    __tablename__ = "alerta_autonoma"

    id = db.Column(db.Integer, primary_key=True)
    codigo_regla = db.Column(db.String(40), nullable=False)
    nivel = db.Column(db.String(20), nullable=False)         # CRITICA | ADVERTENCIA
    titulo = db.Column(db.String(120), nullable=False)
    diagnostico = db.Column(db.Text, nullable=False)
    # Lista JSON de pasos: [{"orden": 1, "accion": "...", "completado": False}]
    protocolo_pasos = db.Column(db.JSON, nullable=False)
    estado = db.Column(db.String(20), default="ACTIVA")      # ACTIVA | RESUELTA
    lectura_id = db.Column(db.Integer, db.ForeignKey("lectura_ambiental.id"), nullable=True)
    timestamp = db.Column(db.DateTime, default=utc_now)

    lectura = db.relationship("LecturaAmbiental", backref="alertas")

    def to_dict(self):
        return {
            "id": self.id,
            "codigo_regla": self.codigo_regla,
            "nivel": self.nivel,
            "titulo": self.titulo,
            "diagnostico": self.diagnostico,
            "protocolo_pasos": self.protocolo_pasos,
            "estado": self.estado,
            "lectura_id": self.lectura_id,
            "timestamp": self.timestamp.isoformat(),
        }
