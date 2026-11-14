# Módulo Entornos Cerrados Hostiles — Informe de triaje autónomo

- **Misión**: Misión Marte de 3 años — hábitat de superficie (`MARS-3Y-01`)
- **Latencia Tierra**: 24 min (ida y vuelta)
- **Modo**: autónomo (sin confirmación de Tierra)
- **Ventana analizada**: `2025-03-03T23:58:00+00:00` → `2025-03-04T23:58:00+00:00`
- **Motor**: `v1.0.0` · 52 reglas evaluadas en 286.7 ms

## Resumen

| Severidad | Alertas visibles | Alarmas derivadas suprimidas |
| --- | --- | --- |
| Crítica | 13 | 27 |
| Advertencia | 0 | — |

## Alertas visibles

### 🔴 RUTA 1: CIRCADIANO → VIRAL

**Qué pasa.** RUTA 1: CIRCADIANO → VIRAL  
**Por qué importa.** Pérdida de vigilancia inmune sobre herpesvirus latentes: prever lesiones en 48–72 h.  
**Regla.** `ROUTE_R1_CIRCADIAN_VIRAL` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| Disparador ambiental (3 noches > 62 dBA o luz azul > 50 lux) | sí | `noches=3 racha=3 Leq_nocturno=59.8 dBA azul_max=68.9066` |
| Respuesta HPA (ratio cortisol AM/NOCHE < 2.0) | sí | `afectados=['CDR-01', 'MS-04', 'PLT-02']` |
| Reactivación viral (>= 1e4 copias/mL) | sí | `detecciones=[('CDR-01', 'VZV_DNA'), ('PLT-02', 'EBV_DNA')]` |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `NOISE_NIGHT_LEQ` | 59.8 | 62 | dBA | 0.33 |
| `BLUE_LIGHT_NIGHT` | 68.91 | 50 | lux@480nm | 0.33 |
| `VIRAL:CDR-01:VZV_DNA` | 12,000 | 10,000 | copias/mL | 0.33 |
| `VIRAL:PLT-02:EBV_DNA` | 35,000 | 10,000 | copias/mL | 0.33 |
| `CORTISOL_RATIO:CDR-01` | 1.7 | 2 | ratio | 0.33 |
| `CORTISOL_RATIO:MS-04` | 1.8 | 2 | ratio | 0.33 |

### 🔴 RUTA 2: HUMEDAD → ALERGIA

**Qué pasa.** RUTA 2: HUMEDAD → ALERGIA  
**Por qué importa.** Sensibilización a antígenos fúngicos: rinitis, dermatitis y broncoespasmo.  
**Regla.** `ROUTE_R2_HUMIDITY_ALLERGY` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| HR > 70 % durante > 12 h | sí | `acumulado=24.0 h` |
| Hongos en aire > 100 UFC/m3 | sí | `max=144.2 UFC/m3 mVOCs=301.9 ppb` |
| Respuesta Th2 o clínica (eosinofilia / IgE / sibilancias / SpO2) | sí | `eos/IgE=['MS-05'] respiratorio=['MS-05'] SpO2_bajo=['MS-05']` |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `HUMIDITY_HORAS_SOBRE_70` | 24 | 12 | h | 0.33 |
| `AIR_FUNGI` | 144.2 | 100 | UFC/m3 | 0.33 |
| `EOSINOFILOS:MS-05` | 640 | 500 | cel/uL | 0.33 |

### 🔴 RUTA 3: BIOPELÍCULA → INFECCIÓN

**Qué pasa.** RUTA 3: BIOPELÍCULA → INFECCIÓN  
**Por qué importa.** Infecciones oportunistas (ITU, gastroenteritis, foliculitis) resistentes al botiquín.  
**Regla.** `ROUTE_R3_BIOFILM_INFECTION` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| Biopelícula en WRS (TOC > 5 mg/L o > 100 UFC/mL o coliformes) | sí | `TOC=6.53 UFC/mL=158.7 coliformes=True` |
| Compromiso inmune (sIgA -40 % o IL-6 > 5 pg/mL en >= 2) | sí | `sIgA=['CDR-01', 'MS-04'] IL6=['CDR-01', 'MS-04', 'MS-05']` |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `WATER_TOC` | 6.53 | 5 | mg/L | 0.50 |
| `WATER_BACTERIA` | 158.7 | 100 | UFC/mL | 0.50 |
| `SIGA:CDR-01` | 88 | 99 | ug/mL | 0.50 |
| `SIGA:MS-04` | 70 | 78.6 | ug/mL | 0.50 |

### 🔴 CONTAMINACIÓN MICROBIANA DEL AGUA

**Qué pasa.** CONTAMINACIÓN MICROBIANA DEL AGUA  
**Por qué importa.** Riesgo alto de ITU y gastroenteritis en 3 tripulante(s): retirar el circuito A de consumo.  
**Regla.** `SCENARIO_A_WATER_IMMUNE` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| Agua fuera de rango (TOC > 5 mg/L o > 100 UFC/mL o coliformes) | sí | `TOC=6.53 UFC/mL=158.7 coliformes=True` |
| Compromiso inmune (sIgA -40 % o IL-6 > 5 pg/mL en >= 2 sujetos) | sí | `sIgA=['CDR-01', 'MS-04'] IL6=['CDR-01', 'MS-04', 'MS-05']` |

**Qué hacer ahora** (lista de verificación secuencial):

| # | Fase | Acción | Verificación |
| --- | --- | --- | --- |
| 1 | AISLAMIENTO | Cerrar las válvulas solenoides del tanque A y conmutar el suministro al Circuito B esterilizado. | PHOTO_MATCH (`QR-VALVE-WRS-04A`) |
| 2 | DESINFECCION | Pasteurizar el circuito aislado a 85 °C durante 60 min y dosificar biocida de plata iónica a 400 ppb. | TACTILE |
| 3 | CLINICO | Protocolo profiláctico de 5 días: zinc 30 mg/día, vitamina D3 4 000 UI/día y probiótico Lactobacillus rhamnosus GG. | QR_SCAN (`QR-ZN30-VITD4000-LGG`) — Cajón 2, Celda B4 |
| 4 | LOGISTICA | Bloquear el consumo del Circuito A hasta dos pruebas sucesivas de ATP/qPCR negativas y empaquetar la telemetría para Tierra. | TACTILE |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `WATER_TOC` | 6.53 | 5 | mg/L | 0.50 |
| `WATER_BACTERIA` | 158.7 | 100 | UFC/mL | 0.50 |
| `SIGA:CDR-01` | 88 | 99 | ug/mL | 0.50 |
| `SIGA:MS-04` | 70 | 78.6 | ug/mL | 0.50 |
| `SIGA:MS-05` | 95 | 72.6 | ug/mL | 0.50 |

### 🔴 REACTIVACIÓN VIRAL POR DISRUPCIÓN CIRCADIANA

**Qué pasa.** REACTIVACIÓN VIRAL POR DISRUPCIÓN CIRCADIANA  
**Por qué importa.** Riesgo de herpes zóster oftálmico o diseminado: iniciar antiviral y resincronizar el eje HPA.  
**Regla.** `SCENARIO_B_CIRCADIAN_VIRAL` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| Ruido nocturno > 62 dBA en >= 3 noches o luz azul > 50 lux | sí | `racha_noches=3 azul_max=68.9066` |
| Ratio cortisol AM/NOCHE < 2.0 | sí | `afectados=['CDR-01', 'MS-04', 'PLT-02']` |
| VZV o EBV > 1e4 copias/mL | sí | `detecciones=[('CDR-01', 'VZV_DNA'), ('PLT-02', 'EBV_DNA')]` |

**Qué hacer ahora** (lista de verificación secuencial):

| # | Fase | Acción | Verificación |
| --- | --- | --- | --- |
| 1 | AISLAMIENTO | Anular el controlador de iluminación: bloquear el espectro < 550 nm durante las 4 h previas al sueño y forzar modo ámbar. | TACTILE |
| 2 | AISLAMIENTO | Activar cancelación activa de ruido en los módulos de descanso y ordenar protectores auditivos de 25 dB de atenuación. | PHOTO_MATCH (`QR-HPD-25DB`) — Cajón 1, Celda A3 |
| 3 | CLINICO | Extraer valaciclovir 1 000 mg cada 8 h durante 7 días del dispensador médico seguro (indicado si VZV > 1e4 copias/mL). | QR_SCAN (`QR-VALAC-1000`) — Cajón 3, Celda A2 |
| 4 | CLINICO | Administrar melatonina de liberación controlada 3 mg una hora antes del ciclo oscuro para resincronizar el eje HPA. | QR_SCAN (`QR-MELAT-3CR`) — Cajón 3, Celda C1 |
| 5 | LOGISTICA | Reclasificar las tareas no críticas del tripulante afectado durante 48 h para reducir la carga alostática. | — |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `NOISE_NIGHTS_OVER_62` | 3 | 3 | noches | 0.34 |
| `BLUE_LIGHT_NIGHT` | 68.91 | 50 | lux@480nm | 0.33 |
| `CORTISOL_RATIO:CDR-01` | 1.7 | 2 | ratio | 0.33 |
| `CORTISOL_RATIO:MS-04` | 1.8 | 2 | ratio | 0.33 |
| `CORTISOL_RATIO:PLT-02` | 1.9 | 2 | ratio | 0.33 |

### 🔴 PROLIFERACIÓN FÚNGICA EN AIRE DE CABINA

**Qué pasa.** PROLIFERACIÓN FÚNGICA EN AIRE DE CABINA  
**Por qué importa.** Riesgo alto de broncoespasmo en 1 tripulante(s); contener la humedad y descontaminar el punto de rocío.  
**Regla.** `SCENARIO_C_FUNGAL_ALLERGY` · score 100/100 · categoría INTEGRATED

| Criterio | ¿Cumple? | Detalle |
| --- | --- | --- |
| HR > 70 % durante > 12 h | sí | `acumulado=24.0 h` |
| Hongos en aire > 100 UFC/m3 | sí | `max=144.2` |
| Eosinófilos > 500/µL o SpO2 < 94 % con sibilancias | sí | `eos=['MS-05'] sibilancias=['MS-05'] SpO2=['MS-05']` |

**Qué hacer ahora** (lista de verificación secuencial):

| # | Fase | Acción | Verificación |
| --- | --- | --- | --- |
| 1 | AISLAMIENTO | Llevar los deshumidificadores de condensación al 100 % y activar recirculación máxima a través de HEPA/ULPA. | — |
| 2 | DESINFECCION | Generar el mapa térmico de humedad para localizar el punto de rocío y limpiar la superficie con toallitas de H2O2 acelerado al 0.5 % (respirador N95 y guantes de nitrilo). | PHOTO_MATCH (`QR-H2O2-05`) — Cajón 5, Celda D1 |
| 3 | CLINICO | Administrar cetirizina 10 mg vía oral y vigilar la respuesta en 60 min. | QR_SCAN (`QR-CETI-10`) — Cajón 4, Celda A1 |
| 4 | CLINICO | Si hay compromiso bronquial: salbutamol inhalado + budesonida inhalada. Evitar corticoides sistémicos orales (agravarían la inmunosupresión espacial). | QR_SCAN (`QR-SALB-BUD`) — Cajón 4, Celda B2 |

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `HUMIDITY_HORAS_SOBRE_70` | 24 | 12 | h | 0.34 |
| `AIR_FUNGI` | 144.2 | 100 | UFC/m3 | 0.33 |
| `EOSINOFILOS:MS-05` | 640 | 500 | cel/uL | 0.33 |

### 🔴 DEGRADACIÓN DEL SISTEMA DE AIRE

**Qué pasa.** DEGRADACIÓN DEL SISTEMA DE AIRE  
**Por qué importa.** 25 alarmas derivadas agrupadas. Fallo primario de revitalización de aire: HEPA/carbón saturados y lecho de CO2 agotado.  
**Regla.** `SYSTEM_AIR_DEGRADED` · score 95/100 · categoría SYSTEM

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `HUMIDITY:HAB-01` | 80.09 | 75 | %HR | 1.00 |
| `HUMIDITY:LAB-01` | 77.54 | 75 | %HR | 1.00 |
| `HUMIDITY:NODE-01` | 81.07 | 75 | %HR | 1.00 |
| `NOISE_LEQ:LAB-01` | 72.56 | 72 | dBA | 1.00 |
| `PPCO2:HAB-01` | 0.68 | 0.53 | kPa | 1.00 |
| `PPCO2:LAB-01` | 0.73 | 0.53 | kPa | 1.00 |

### 🔴 DEGRADACIÓN DEL SISTEMA DE AGUA

**Qué pasa.** DEGRADACIÓN DEL SISTEMA DE AGUA  
**Por qué importa.** 2 alarmas derivadas agrupadas. Biopelícula en el WRS: retirar el circuito afectado del consumo.  
**Regla.** `SYSTEM_WATER_DEGRADED` · score 95/100 · categoría SYSTEM

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `WATER_BACTERIA:A` | 158.7 | 100 | UFC/mL | 1.00 |
| `WATER_TOC:A` | 6.53 | 5 | mg/L | 1.00 |

### 🔴 REACTIVACIÓN VIRAL (4)

**Qué pasa.** REACTIVACIÓN VIRAL (4)  
**Por qué importa.** Riesgo de herpes zóster, mononucleosis atípica o encefalitis.  
**Regla.** `BIO_VIRAL_REACTIVATION` · score 75/100 · categoría IMMUNO

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `VZV_DNA:CDR-01` | 12,000 | 10,000 | copias/mL | 1.00 |
| `EBV_DNA:PLT-02` | 35,000 | 10,000 | copias/mL | 1.00 |
| `EBV_DNA:CDR-01` | 4,000 | 10,000 | copias/mL | 0.40 |
| `VZV_DNA:MS-04` | 8,000 | 10,000 | copias/mL | 0.40 |

### 🔴 ALTERACIÓN DEL LEUCOGRAMA

**Qué pasa.** ALTERACIÓN DEL LEUCOGRAMA  
**Por qué importa.** Neutrofilia con linfopenia (infección) o eosinofilia (hipersensibilidad).  
**Regla.** `BIO_DIFFERENTIAL_SHIFT` · score 70/100 · categoría IMMUNO

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `EOSINOFILOS:MS-05` | 640 | 500 | cel/uL | 1.00 |
| `NEUTROFILIA+LYMPHOPENIA:MS-04` | 78 | 75 | % neutrófilos | 0.90 |

### 🔴 sIgA EN CAÍDA (3 TRIPULANTES)

**Qué pasa.** sIgA EN CAÍDA (3 TRIPULANTES)  
**Por qué importa.** Vulnerabilidad de la barrera mucosa: puerta de entrada a infecciones oportunistas.  
**Regla.** `BIO_SIGA_DROP` · score 70/100 · categoría IMMUNO

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `SIGA:CDR-01` | 88 | 99 | ug/mL | 1.00 |
| `SIGA:MS-04` | 70 | 78.6 | ug/mL | 1.00 |
| `SIGA:MS-05` | 95 | 72.6 | ug/mL | 0.54 |

### 🔴 CURVA DE CORTISOL APLANADA (4)

**Qué pasa.** CURVA DE CORTISOL APLANADA (4)  
**Por qué importa.** Hiperactividad del eje HPA: supresión Th1 y menor vigilancia inmune.  
**Regla.** `BIO_CORTISOL_FLATTENING` · score 65/100 · categoría IMMUNO

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `CORTISOL_RATIO:CDR-01` | 1.7 | 2 | ratio AM/NOCHE | 1.00 |
| `CORTISOL_RATIO:MS-04` | 1.8 | 2 | ratio AM/NOCHE | 1.00 |
| `CORTISOL_RATIO:PLT-02` | 1.9 | 2 | ratio AM/NOCHE | 1.00 |
| `CORTISOL_RATIO:MS-05` | 2.3 | 2 | ratio AM/NOCHE | 0.50 |
| `CORTISOL_DIAS_APLANADOS:CDR-01` | 2 | 3 | días | 0.40 |
| `CORTISOL_DIAS_APLANADOS:MS-04` | 2 | 3 | días | 0.40 |

### 🔴 PERFIL INFLAMATORIO Th2 (3 CON IL-6)

**Qué pasa.** PERFIL INFLAMATORIO Th2 (3 CON IL-6)  
**Por qué importa.** Inflamación sistémica de bajo grado e inmunosenescencia acelerada.  
**Regla.** `BIO_CYTOKINE_SHIFT` · score 60/100 · categoría IMMUNO

**Evidencia**

| Métrica | Observado | Umbral | Unidad | Peso |
| --- | --- | --- | --- | --- |
| `IL6:MS-04` | 7.4 | 5 | pg/mL | 0.74 |
| `IFNG_IL10:CDR-01` | 0.48 | 1 | ratio | 0.70 |
| `IFNG_IL10:MS-04` | 0.38 | 1 | ratio | 0.70 |
| `IFNG_IL10:PLT-02` | 0.66 | 1 | ratio | 0.70 |
| `IL6:CDR-01` | 6.8 | 5 | pg/mL | 0.68 |
| `IL6:MS-05` | 5.6 | 5 | pg/mL | 0.56 |

## Alarmas derivadas suprimidas

Agrupadas bajo su causa raíz para evitar la cascada de alarmas. Siguen siendo consultables y auditables; no se han borrado.

| Causa raíz | Alarma derivada | Severidad | Valor |
| --- | --- | --- | --- |
| `SYSTEM_AIR_DEGRADED` | BLUE_LIGHT_NIGHT ADVERTENCIA EN HAB-01 | WARNING | 67.23 lux@480nm |
| `SYSTEM_AIR_DEGRADED` | BLUE_LIGHT_NIGHT ADVERTENCIA EN LAB-01 | WARNING | 67.12 lux@480nm |
| `SYSTEM_AIR_DEGRADED` | BLUE_LIGHT_NIGHT ADVERTENCIA EN NODE-01 | WARNING | 66.86 lux@480nm |
| `SYSTEM_AIR_DEGRADED` | HUMIDITY CRÍTICO EN HAB-01 | CRITICAL | 80.09 %HR |
| `SYSTEM_AIR_DEGRADED` | HUMIDITY CRÍTICO EN LAB-01 | CRITICAL | 77.54 %HR |
| `SYSTEM_AIR_DEGRADED` | HUMIDITY CRÍTICO EN NODE-01 | CRITICAL | 81.07 %HR |
| `SYSTEM_AIR_DEGRADED` | LIGHT_NIGHT ADVERTENCIA EN HAB-01 | WARNING | 32.08 lux |
| `SYSTEM_AIR_DEGRADED` | LIGHT_NIGHT ADVERTENCIA EN LAB-01 | WARNING | 31.96 lux |
| `SYSTEM_AIR_DEGRADED` | LIGHT_NIGHT ADVERTENCIA EN NODE-01 | WARNING | 32.15 lux |
| `SYSTEM_AIR_DEGRADED` | NOISE_LEQ ADVERTENCIA EN HAB-01 | WARNING | 69.59 dBA |
| `SYSTEM_AIR_DEGRADED` | NOISE_LEQ CRÍTICO EN LAB-01 | CRITICAL | 72.56 dBA |
| `SYSTEM_AIR_DEGRADED` | NOISE_LEQ ADVERTENCIA EN NODE-01 | WARNING | 71.11 dBA |
| `SYSTEM_AIR_DEGRADED` | PPCO2 CRÍTICO EN HAB-01 | CRITICAL | 0.68 kPa |
| `SYSTEM_AIR_DEGRADED` | PPCO2 CRÍTICO EN LAB-01 | CRITICAL | 0.73 kPa |
| `SYSTEM_AIR_DEGRADED` | PPCO2 CRÍTICO EN NODE-01 | CRITICAL | 0.66 kPa |
| `SYSTEM_AIR_DEGRADED` | TEMPERATURE ADVERTENCIA EN HAB-01 | WARNING | 27.01 C |
| `SYSTEM_AIR_DEGRADED` | TEMPERATURE ADVERTENCIA EN LAB-01 | WARNING | 26.3 C |
| `SYSTEM_AIR_DEGRADED` | TEMPERATURE ADVERTENCIA EN NODE-01 | WARNING | 27.52 C |
| `SYSTEM_AIR_DEGRADED` | VOC_TVOC ADVERTENCIA EN HAB-01 | WARNING | 889.32 ppb |
| `SYSTEM_AIR_DEGRADED` | VOC_TVOC ADVERTENCIA EN LAB-01 | WARNING | 889.33 ppb |
| `SYSTEM_AIR_DEGRADED` | VOC_TVOC ADVERTENCIA EN NODE-01 | WARNING | 889.42 ppb |
| `SYSTEM_AIR_DEGRADED` | BIOAEROSOL BACTERIANO ELEVADO | WARNING | 796 UFC/m3 |
| `SYSTEM_AIR_DEGRADED` | CARGA FÚNGICA AÉREA ELEVADA | CRITICAL | 144.2 UFC/m3 |
| `SYSTEM_AIR_DEGRADED` | PATÓGENO OPORTUNISTA EN SUPERFICIE | CRITICAL | 1 deteccion |
| `SYSTEM_AIR_DEGRADED` | BIOCARGA ALTA EN SUPERFICIE CRÍTICA | WARNING | 348.5 UFC/100cm2 |
| `SYSTEM_WATER_DEGRADED` | BACTERIAS EN AGUA POTABLE | CRITICAL | 158.7 UFC/mL |
| `SYSTEM_WATER_DEGRADED` | TOC ELEVADO EN EL WRS | CRITICAL | 6.53 mg/L |

## Acciones autónomas registradas

| Alerta | # | Fase | Acción | Estado |
| --- | --- | --- | --- | --- |
| `SCENARIO_A_WATER_IMMUNE` | 1 | AISLAMIENTO | `CLOSE_SOLENOID_VALVE` | PENDING |
| `SCENARIO_A_WATER_IMMUNE` | 2 | DESINFECCION | `THERMAL_PASTEURIZATION` | PENDING |
| `SCENARIO_A_WATER_IMMUNE` | 3 | CLINICO | `MUCOSAL_PROPHYLAXIS_5D` | PENDING |
| `SCENARIO_A_WATER_IMMUNE` | 4 | LOGISTICA | `QUARANTINE_CIRCUIT_AND_DOWNLINK` | PENDING |
| `SCENARIO_B_CIRCADIAN_VIRAL` | 1 | AISLAMIENTO | `LIGHTING_SPECTRUM_OVERRIDE` | PENDING |
| `SCENARIO_B_CIRCADIAN_VIRAL` | 2 | AISLAMIENTO | `NOISE_CANCELLATION_AND_HPD` | PENDING |
| `SCENARIO_B_CIRCADIAN_VIRAL` | 3 | CLINICO | `ANTIVIRAL_VALACYCLOVIR` | PENDING |
| `SCENARIO_B_CIRCADIAN_VIRAL` | 4 | CLINICO | `MELATONIN_CR` | PENDING |
| `SCENARIO_B_CIRCADIAN_VIRAL` | 5 | LOGISTICA | `RECLASSIFY_TASKS_48H` | PENDING |
| `SCENARIO_C_FUNGAL_ALLERGY` | 1 | AISLAMIENTO | `MAX_DEHUMIDIFICATION_RECIRC` | PENDING |
| `SCENARIO_C_FUNGAL_ALLERGY` | 2 | DESINFECCION | `LOCALIZED_DECON_H2O2` | PENDING |
| `SCENARIO_C_FUNGAL_ALLERGY` | 3 | CLINICO | `ANTIHISTAMINE_2ND_GEN` | PENDING |
| `SCENARIO_C_FUNGAL_ALLERGY` | 4 | CLINICO | `INHALED_BRONCHODILATOR_STEROID` | PENDING |

## Cola de transmisión a Tierra

| Creado | Tipo | Estado | Bytes |
| --- | --- | --- | --- |
| 2026-11-14T15:01:40Z | ALERT_BUNDLE | QUEUED | 2,767 |
| 2026-11-14T15:01:40Z | ALERT_BUNDLE | QUEUED | 3,279 |
| 2026-11-14T15:01:40Z | ALERT_BUNDLE | QUEUED | 2,533 |
| 2026-11-14T15:10:12Z | ALERT_BUNDLE | QUEUED | 2,767 |
| 2026-11-14T15:10:12Z | ALERT_BUNDLE | QUEUED | 3,279 |
| 2026-11-14T15:10:12Z | ALERT_BUNDLE | QUEUED | 2,533 |
