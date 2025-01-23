from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from decouple import config
from utils import get_cancun_time
from datetime import datetime
from prompt import generate_openai_prompt
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from buscarslot import find_next_available_slot
from consultarinfo import read_sheet_data
import requests
import openai

# **SECCIÓN 1: Configuración del sistema**
CHATGPT_SECRET_KEY = config("CHATGPT_SECRET_KEY")
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = config("TWILIO_PHONE")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

# Inicializamos la aplicación FastAPI
app = FastAPI()

# **SECCIÓN 2: Endpoint raíz**
@app.get("/")
def read_root():
    """
    Ruta raíz para verificar que el servicio está activo.
    """
    return {"message": "El servicio está funcionando correctamente"}

# **SECCIÓN 3: Endpoint para consultar información desde Google Sheets**
@app.get("/consultar-informacion")
def consultar_informacion():
    """
    Endpoint para leer y devolver datos desde Google Sheets.
    """
    try:
        data = read_sheet_data()
        return JSONResponse({"message": "Información obtenida con éxito", "data": data})
    except Exception as e:
        return JSONResponse({"error": "Error al consultar la información", "details": str(e)}, status_code=500)

# **SECCIÓN 4: Endpoint para crear citas**
@app.post("/crear-cita")
async def crear_cita(request: Request):
    """
    Endpoint para crear una nueva cita en Google Calendar.
    """
    try:
        data = await request.json()
        name = data.get("name")
        phone = data.get("phone")
        reason = data.get("reason", "No especificado")
        start_time = datetime.fromisoformat(data.get("start_time"))
        end_time = datetime.fromisoformat(data.get("end_time"))

        # Crear cita en Google Calendar
        event = create_calendar_event(name, phone, reason, start_time, end_time)
        return JSONResponse({"message": "Cita creada con éxito", "event": event})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "Error al crear la cita"}, status_code=500)

# **SECCIÓN 5: Endpoint para editar citas**
@app.put("/editar-cita")
async def editar_cita(request: Request):
    """
    Endpoint para editar una cita existente en Google Calendar.
    """
    try:
        data = await request.json()
        phone = data.get("phone")
        original_start_time = datetime.fromisoformat(data.get("original_start_time"))
        new_start_time = data.get("new_start_time")
        new_end_time = data.get("new_end_time")

        if new_start_time and new_end_time:
            new_start_time = datetime.fromisoformat(new_start_time)
            new_end_time = datetime.fromisoformat(new_end_time)

        # Editar la cita
        result = edit_calendar_event(phone, original_start_time, new_start_time, new_end_time)
        return JSONResponse({"message": "Cita editada con éxito", "result": result})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "Error al editar la cita"}, status_code=500)

# **SECCIÓN 6: Endpoint para eliminar citas**
@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    """
    Endpoint para eliminar una cita existente en Google Calendar.
    """
    try:
        data = await request.json()
        phone = data.get("phone")
        patient_name = data.get("patient_name", None)

        # Eliminar cita
        result = delete_calendar_event(phone, patient_name)
        return JSONResponse({"message": "Cita eliminada con éxito", "result": result})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "Error al eliminar la cita"}, status_code=500)

# **SECCIÓN 7: Endpoint para buscar el próximo slot disponible**
@app.get("/buscar-slot")
def buscar_slot():
    """
    Endpoint para buscar el próximo horario disponible en Google Calendar.
    """
    try:
        slot = find_next_available_slot()
        if slot:
            return JSONResponse({"message": "Slot disponible encontrado", "slot": slot})
        else:
            return JSONResponse({"message": "No se encontraron horarios disponibles"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": "Error al buscar el slot disponible"}, status_code=500)
