from twilio.twiml.voice_response import VoiceResponse
from fastapi import FastAPI, Request, Response
from tw_utils import handle_twilio_call, process_user_input  # Eliminar end_twilio_call
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
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

# Middleware para medir tiempos de endpoints
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    logger.info(f"Endpoint: {request.url.path} | Tiempo: {duration:.2f}s")
    return response

# Endpoint para servir audio
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"
@app.get("/audio-response")
async def get_audio():
    try:
        with open(AUDIO_TEMP_PATH, "rb") as f:
            return Response(content=f.read(), media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"Error sirviendo audio: {str(e)}")
        return Response(content="Audio no disponible", status_code=404)

# Endpoints principales
@app.get("/")
def read_root():
    return {"message": "El servicio está funcionando correctamente"}

# Modificar endpoints de Twilio
@app.post("/twilio-call")
async def twilio_call(request: Request):
    try:
        response = VoiceResponse()
        response.redirect("/process-user-input")
        return Response(content=str(response), media_type="text/xml")
    except Exception as e:
        logger.error(f"Error en Twilio Call: {str(e)}")
        return Response(content=str(e), status_code=500)

@app.post("/process-user-input")
async def twilio_process_input(request: Request):
    try:
        form_data = await request.form()
        user_input = form_data.get("SpeechResult", "")
        
        # Procesar entrada y generar respuesta
        twilio_response = await process_user_input(user_input)
        
        return Response(content=twilio_response, media_type="text/xml")
        
    except Exception as e:
        logger.error(f"Error en Process Input: {str(e)}")
        return Response(content=str(e), status_code=500)

# [Mantener el resto de endpoints sin cambios...]















# Mantenemos todos tus otros endpoints sin cambios
@app.get("/consultar-informacion")
def consultar_informacion():
    try:
        data = read_sheet_data()
        return {"message": "Información obtenida con éxito", "data": data}
    except Exception as e:
        return {"error": "Error al consultar la información", "details": str(e)}

@app.get("/buscar-slot")
def buscar_slot():
    try:
        slot = find_next_available_slot()
        if slot:
            return {"message": "Slot disponible encontrado", "slot": slot}
        else:
            return {"message": "No se encontraron horarios disponibles"}
    except Exception as e:
        return {"error": "Error al buscar el slot disponible", "details": str(e)}

@app.post("/crear-cita")
async def crear_cita(request: Request):
    try:
        data = await request.json()
        name = data.get("name")
        phone = data.get("phone")
        reason = data.get("reason", "No especificado")
        start_time = data.get("start_time")
        end_time = data.get("end_time")

        if not name or not phone or not start_time or not end_time:
            raise ValueError("Los campos 'name', 'phone', 'start_time' y 'end_time' son obligatorios.")

        start_time = datetime.fromisoformat(start_time)
        end_time = datetime.fromisoformat(end_time)

        event = create_calendar_event(name, phone, reason, start_time, end_time)
        return {"message": "Cita creada con éxito", "event": event}
    except Exception as e:
        return {"error": "Error al crear la cita", "details": str(e)}

@app.put("/editar-cita")
async def editar_cita(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        original_start_time = datetime.fromisoformat(data.get("original_start_time"))
        new_start_time = data.get("new_start_time")
        new_end_time = data.get("new_end_time")

        if new_start_time and new_end_time:
            new_start_time = datetime.fromisoformat(new_start_time)
            new_end_time = datetime.fromisoformat(new_end_time)

        result = edit_calendar_event(phone, original_start_time, new_start_time, new_end_time)
        return {"message": "Cita editada con éxito", "result": result}
    except Exception as e:
        return {"error": "Error al editar la cita", "details": str(e)}

@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    try:
        data = await request.json()
        phone = data.get("phone")
        patient_name = data.get("patient_name", None)
        result = delete_calendar_event(phone, patient_name)
        return {"message": "Cita eliminada con éxito", "result": result}
    except Exception as e:
        return {"error": "Error al eliminar la cita", "details": str(e)}