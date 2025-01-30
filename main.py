from fastapi import FastAPI, Request, Response
from tw_utils import handle_twilio_call, process_user_input
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from datetime import datetime
import os

app = FastAPI()

# Configuración para el audio (agregado)
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"

@app.get("/")
def read_root():
    return {"message": "El servicio está funcionando correctamente"}

# Nuevo endpoint para servir el audio (agregado)
@app.get("/audio-response")
async def get_audio():
    if os.path.exists(AUDIO_TEMP_PATH):
        with open(AUDIO_TEMP_PATH, "rb") as f:
            return Response(content=f.read(), media_type="audio/mpeg")
    return Response(content="Audio no disponible", status_code=404)

# Endpoints de Twilio (corregidos)
@app.post("/twilio-call")
async def twilio_call(request: Request):
    try:
        twilio_response = handle_twilio_call(gather_action="/process-user-input")
        return Response(content=twilio_response, media_type="text/xml")
    except Exception as e:
        return Response(content=f"Error en Twilio: {str(e)}", status_code=500)

@app.post("/process-user-input")
async def twilio_process_input(request: Request):
    try:
        form_data = await request.form()
        user_input = form_data.get("SpeechResult", "")
        twilio_response = process_user_input(user_input)
        return Response(content=twilio_response, media_type="text/xml")
    except Exception as e:
        return Response(content=f"Error procesando entrada: {str(e)}", status_code=500)

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