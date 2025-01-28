from fastapi import FastAPI, Request
from tw_utils import handle_twilio_call, process_user_input
from aiagent import generate_openai_response
from labs_utils import generate_audio_with_eleven_labs
from buscarslot import find_next_available_slot
from consultarinfo import read_sheet_data
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from datetime import datetime

# Inicializamos la aplicación FastAPI
app = FastAPI()

@app.get("/")
def read_root():
    """
    Verifica que el servicio esté funcionando.
    """
    return {"message": "El servicio está funcionando correctamente"}

# **ENDPOINTS DE TWILIO**
@app.post("/twilio-call")
async def twilio_call(request: Request):
    """
    Maneja la llamada entrante desde Twilio.
    """
    return await handle_twilio_call(request)

@app.post("/process-user-input")
async def twilio_process_input(request: Request):
    """
    Procesa la entrada del usuario enviada por Twilio.
    """
    return await process_user_input(request)

# **ENDPOINT PARA CONSULTAR INFORMACIÓN**
@app.get("/consultar-informacion")
def consultar_informacion():
    """
    Consulta información desde Google Sheets.
    """
    try:
        data = read_sheet_data()
        return {"message": "Información obtenida con éxito", "data": data}
    except Exception as e:
        return {"error": "Error al consultar la información", "details": str(e)}

# **ENDPOINT PARA BUSCAR EL PRÓXIMO SLOT DISPONIBLE**
@app.get("/buscar-slot")
def buscar_slot():
    """
    Busca el próximo slot disponible en el calendario.
    """
    try:
        slot = find_next_available_slot()
        if slot:
            return {"message": "Slot disponible encontrado", "slot": slot}
        else:
            return {"message": "No se encontraron horarios disponibles"}
    except Exception as e:
        return {"error": "Error al buscar el slot disponible", "details": str(e)}

# **ENDPOINT PARA CREAR UNA CITA**
@app.post("/crear-cita")
async def crear_cita(request: Request):
    """
    Crea una nueva cita en Google Calendar.
    """
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

# **ENDPOINT PARA EDITAR UNA CITA**
@app.put("/editar-cita")
async def editar_cita(request: Request):
    """
    Edita una cita existente en Google Calendar.
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

        result = edit_calendar_event(phone, original_start_time, new_start_time, new_end_time)
        return {"message": "Cita editada con éxito", "result": result}
    except Exception as e:
        return {"error": "Error al editar la cita", "details": str(e)}

# **ENDPOINT PARA ELIMINAR UNA CITA**
@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    """
    Elimina una cita existente en Google Calendar.
    """
    try:
        data = await request.json()
        phone = data.get("phone")
        patient_name = data.get("patient_name", None)

        result = delete_calendar_event(phone, patient_name)
        return {"message": "Cita eliminada con éxito", "result": result}
    except Exception as e:
        return {"error": "Error al eliminar la cita", "details": str(e)}
