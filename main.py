# -*- coding: utf-8 -*-
"""
Archivo principal para la API basada en FastAPI.
Gestiona las interacciones con Twilio, Google Calendar y Google Sheets.
"""

# ==================================================
# 📌 Importaciones y Configuración
# ==================================================
from fastapi import FastAPI, Request, Response, HTTPException
from twilio.twiml.voice_response import VoiceResponse
from tw_utils import handle_twilio_call, process_user_input
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot  # Corregido nombre del módulo
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from datetime import datetime
import os
import logging
import time

app = FastAPI()

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================
# 🔹 Middleware para medir tiempos de respuesta
# ==================================================
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    logger.info(f"{request.method} {request.url.path} | Tiempo: {duration:.2f}s")
    return response

# ==================================================
# 🔹 Función de validación de teléfono
# ==================================================
def validate_phone(phone: str):
    """
    Valida que el número de teléfono tenga 10 dígitos numéricos.

    Parámetros:
        phone (str): Número de teléfono ingresado.

    Retorna:
        bool: True si es válido, False si no lo es.
    """
    return phone.isdigit() and len(phone) == 10

# ==================================================
# 🔹 Endpoint para servir audio
# ==================================================
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"

@app.get("/audio-response")
async def get_audio():
    try:
        with open(AUDIO_TEMP_PATH, "rb") as f:
            return Response(content=f.read(), media_type="audio/mpeg")
    except FileNotFoundError:
        logger.error("Archivo de audio no encontrado")
        raise HTTPException(status_code=404, detail="Audio no disponible")

# ==================================================
# 🔹 Endpoints Principales
# ==================================================
@app.get("/")
def read_root():
    return {"message": "El servicio está funcionando correctamente"}

@app.post("/twilio-call")
async def twilio_call():
    try:
        return Response(content=await handle_twilio_call("/process-user-input"), media_type="text/xml")
    except Exception as e:
        logger.error(f"Error en Twilio Call: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/process-user-input")
async def twilio_process_input(request: Request):
    try:
        twilio_response = await process_user_input(request)  # ✅ Ahora pasamos el request completo
        return Response(content=twilio_response, media_type="text/xml")
    except Exception as e:
        logger.error(f"Error en Process Input: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))










# ==================================================
# 🔹 Google Sheets - Consultar Información
# ==================================================
@app.get("/consultar-informacion")
def consultar_informacion():
    try:
        data = read_sheet_data()
        return {"message": "Información obtenida con éxito", "data": data}
    except Exception as e:
        logger.error(f"Error al consultar la información: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al obtener información")

# ==================================================
# 🔹 Google Calendar - Buscar Slots Disponibles
# ==================================================
@app.get("/buscar-slot")
def buscar_slot():
    try:
        slot = find_next_available_slot()
        if slot:
            return {"message": "Slot disponible encontrado", "slot": slot}
        return {"message": "No se encontraron horarios disponibles"}
    except Exception as e:
        logger.error(f"Error al buscar slot: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al buscar slot")

# ==================================================
# 🔹 Google Calendar - Crear Cita
# ==================================================
@app.post("/crear-cita")
async def crear_cita(request: Request):
    try:
        data = await request.json()
        required_fields = ["name", "phone", "start_time", "end_time"]

        for field in required_fields:
            if field not in data or not data[field]:
                raise HTTPException(status_code=400, detail=f"Falta el campo obligatorio: {field}")

        if not validate_phone(data["phone"]):
            raise HTTPException(status_code=400, detail="El teléfono debe tener 10 dígitos numéricos")

        # Convertir formato ISO correctamente
        event = create_calendar_event(
            data["name"],
            data["phone"],
            data.get("reason", "No especificado"),
            datetime.fromisoformat(data["start_time"]),
            datetime.fromisoformat(data["end_time"])
        )

        return {"message": "Cita creada", "event": event}
    except Exception as e:
        logger.error(f"Error al crear cita: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================================================
# 🔹 Google Calendar - Editar Cita
# ==================================================
@app.put("/editar-cita")
async def editar_cita(request: Request):
    try:
        data = await request.json()
        required_fields = ["phone", "original_start_time"]
        
        for field in required_fields:
            if field not in data or not data[field]:
                raise HTTPException(status_code=400, detail=f"Falta el campo obligatorio: {field}")

        if not validate_phone(data["phone"]):
            raise HTTPException(status_code=400, detail="El teléfono debe tener 10 dígitos numéricos")

        result = edit_calendar_event(
            data["phone"],
            datetime.fromisoformat(data["original_start_time"]),
            datetime.fromisoformat(data["new_start_time"]) if "new_start_time" in data else None,
            datetime.fromisoformat(data["new_end_time"]) if "new_end_time" in data else None
        )

        return {"message": "Cita editada", "result": result}
    except Exception as e:
        logger.error(f"Error al editar cita: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# ==================================================
# 🔹 Google Calendar - Eliminar Cita
# ==================================================
@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    try:
        data = await request.json()
        
        if "phone" not in data or not data["phone"]:
            raise HTTPException(status_code=400, detail="El teléfono es obligatorio para eliminar una cita")

        if not validate_phone(data["phone"]):
            raise HTTPException(status_code=400, detail="El teléfono debe tener 10 dígitos numéricos")

        result = delete_calendar_event(data["phone"], data.get("patient_name"))
        return {"message": "Cita eliminada", "result": result}
    except Exception as e:
        logger.error(f"Error al eliminar cita: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))