from twilio.twiml.voice_response import VoiceResponse
from fastapi import FastAPI, Request, Response
from tw_utils import handle_twilio_call, process_user_input
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

@app.get("/")
def read_root():
    return {"message": "El servicio está funcionando correctamente"}

@app.post("/twilio-call")
async def twilio_call(request: Request):
    try:
        return Response(content=handle_twilio_call("/process-user-input"), media_type="text/xml")
    except Exception as e:
        logger.error(f"Error en Twilio Call: {str(e)}")
        return Response(content=str(e), status_code=500)

@app.post("/process-user-input")
async def twilio_process_input(request: Request):
    try:
        form_data = await request.form()
        user_input = form_data.get("SpeechResult", "")
        twilio_response = await process_user_input(user_input)
        return Response(content=twilio_response, media_type="text/xml")
    except Exception as e:
        logger.error(f"Error en Process Input: {str(e)}")
        return Response(content=str(e), status_code=500)

# Endpoints adicionales (mantenidos sin cambios)
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
        return {"message": "Slot disponible encontrado", "slot": slot} if slot else {"message": "No se encontraron horarios disponibles"}
    except Exception as e:
        return {"error": "Error al buscar slot", "details": str(e)}

@app.post("/crear-cita")
async def crear_cita(request: Request):
    try:
        data = await request.json()
        required_fields = ["name", "phone", "start_time", "end_time"]
        if not all(data.get(field) for field in required_fields):
            raise ValueError("Faltan campos obligatorios")
        
        event = create_calendar_event(
            data["name"],
            data["phone"],
            data.get("reason", "No especificado"),
            datetime.fromisoformat(data["start_time"]),
            datetime.fromisoformat(data["end_time"])
        )
        return {"message": "Cita creada", "event": event}
    except Exception as e:
        return {"error": str(e)}, 400

@app.put("/editar-cita")
async def editar_cita(request: Request):
    try:
        data = await request.json()
        result = edit_calendar_event(
            data["phone"],
            datetime.fromisoformat(data["original_start_time"]),
            datetime.fromisoformat(data["new_start_time"]) if data.get("new_start_time") else None,
            datetime.fromisoformat(data["new_end_time"]) if data.get("new_end_time") else None
        )
        return {"message": "Cita editada", "result": result}
    except Exception as e:
        return {"error": str(e)}, 400

@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    try:
        data = await request.json()
        result = delete_calendar_event(data["phone"], data.get("patient_name"))
        return {"message": "Cita eliminada", "result": result}
    except Exception as e:
        return {"error": str(e)}, 400