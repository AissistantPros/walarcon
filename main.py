# main.py
import os
import logging
from fastapi import FastAPI, Response, WebSocket
from tw_utils import TwilioWebSocketManager, set_debug   
from consultarinfo import get_consultorio_data_from_cache, load_consultorio_data_to_cache # Añadido load_consultorio_data_to_cache
from consultarinfo import router as consultorio_router # Importamos el router
from fastapi import Body # Esta línea la necesitas para que FastAPI reciba datos
import buscarslot       # Para poder usar tu lógica de buscarslot.py
from typing import Optional, Union # Esto es para definir tipos de datos, ayuda a que el código sea más claro
from crearcita import create_calendar_event # Para crear citas
from editarcita import edit_calendar_event   # Para editar citas
from eliminarcita import delete_calendar_event # Para eliminar citas
from selectevent import select_calendar_event_by_index
from utils import search_calendar_event_by_phone # Para seleccionar evento por índice

# ───────── CONFIGURACIÓN DE LOGGING ────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
                    datefmt="%H:%M:%S")


logger = logging.getLogger(__name__)

# Silenciamos verbosidad de librerías externas
# NIVEL PARA LIBRERÍAS EXTERNAS “NOISY”
for noisy in (
    "openai._base_client",    # peticiones HTTP a OpenAI
    "httpcore.http11",        # tráfico httpx-httpcore
    "httpcore.connection",
    "httpx",
    "websockets.client",
    "websockets.server",
    # Añade aquí cualquier otro módulo que genere ruido que quieras eliminar:
    "urllib3.connectionpool",
    "asyncio",
    "uvicorn.access",
    "uvicorn.error",
    "deepgram.clients.common.v1.abstract_async_websocket",
    "fastapi",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)




# ───────── FASTAPI ─────────────────────────────────────────────
app = FastAPI()


app.include_router(consultorio_router, prefix="/api_v1") # Puedes elegir un prefijo o no



@app.post("/n8n/process-appointment-request") # Usamos POST porque enviaremos datos
async def n8n_process_appointment_request(
    user_query_for_date_time: str = Body(...),
    day_param: Optional[int] = Body(None),
    month_param: Optional[Union[str, int]] = Body(None),
    year_param: Optional[int] = Body(None),
    fixed_weekday_param: Optional[str] = Body(None),
    explicit_time_preference_param: Optional[str] = Body(None),
    is_urgent_param: Optional[bool] = Body(False),
    more_late_param: Optional[bool] = Body(False),
    more_early_param: Optional[bool] = Body(False)
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/process-appointment-request con query: {user_query_for_date_time}")
    try:
        # Llamamos directamente a la función de buscarslot con los parámetros recibidos
        result = buscarslot.process_appointment_request(
            user_query_for_date_time=user_query_for_date_time,
            day_param=day_param,
            month_param=month_param,
            year_param=year_param,
            fixed_weekday_param=fixed_weekday_param,
            explicit_time_preference_param=explicit_time_preference_param,
            is_urgent_param=is_urgent_param,
            more_late_param=more_late_param,
            more_early_param=more_early_param
        )
        return result # La función ya devuelve un diccionario JSON
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/process-appointment-request: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}
    





@app.post("/n8n/create-calendar-event")
async def n8n_create_calendar_event(
    name: str = Body(...),
    phone: str = Body(...),
    reason: str = Body(...),
    start_time: str = Body(...),
    end_time: str = Body(...)
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/create-calendar-event para {name}")
    try:
        result = create_calendar_event(
            name=name,
            phone=phone,
            reason=reason,
            start_time=start_time,
            end_time=end_time
        )
        if "error" in result:
            logger.error(f"❌ Error al crear evento: {result['error']}")
            return {"status": "ERROR", "message": result["error"]}
        return {"status": "SUCCESS", "event_id": result.get("id"), "message": "Evento creado exitosamente"}
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/create-calendar-event: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}

@app.post("/n8n/edit-calendar-event")
async def n8n_edit_calendar_event(
    event_id: str = Body(...),
    new_start_time_iso: str = Body(...),
    new_end_time_iso: str = Body(...),
    new_name: Optional[str] = Body(None),
    new_reason: Optional[str] = Body(None),
    new_phone_for_description: Optional[str] = Body(None)
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/edit-calendar-event para ID: {event_id}")
    try:
        result = edit_calendar_event(
            event_id=event_id,
            new_start_time_iso=new_start_time_iso,
            new_end_time_iso=new_end_time_iso,
            new_name=new_name,
            new_reason=new_reason,
            new_phone_for_description=new_phone_for_description
        )
        if "error" in result:
            logger.error(f"❌ Error al editar evento: {result['error']}")
            return {"status": "ERROR", "message": result["error"]}
        return {"status": "SUCCESS", "event_id": result.get("id"), "message": "Evento editado exitosamente"}
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/edit-calendar-event: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}

@app.post("/n8n/delete-calendar-event")
async def n8n_delete_calendar_event(
    event_id: str = Body(...),
    original_start_time_iso: str = Body(...) # Aunque no es estrictamente necesario para la función, FastAPI lo espera por la definición de la tool
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/delete-calendar-event para ID: {event_id}")
    try:
        result = delete_calendar_event(
            event_id=event_id,
            original_start_time_iso=original_start_time_iso # Se pasa aunque la función no lo use para la operación principal
        )
        if "error" in result:
            logger.error(f"❌ Error al eliminar evento: {result['error']}")
            return {"status": "ERROR", "message": result["error"]}
        return {"status": "SUCCESS", "deleted_event_id": result.get("deleted_event_id"), "message": "Evento eliminado exitosamente"}
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/delete-calendar-event: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}


@app.post("/n8n/search-calendar-event-by-phone")
async def n8n_search_calendar_event_by_phone(
    phone: str = Body(...)
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/search-calendar-event-by-phone para teléfono: {phone}")
    try:
        # La función search_calendar_event_by_phone ya devuelve una lista de diccionarios
        search_results = search_calendar_event_by_phone(phone=phone)
        return {"search_results": search_results} # Envolvemos la lista en un diccionario
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/search-calendar-event-by-phone: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}

@app.post("/n8n/select-calendar-event-by-index")
async def n8n_select_calendar_event_by_index(
    selected_index: int = Body(...)
):
    logger.info(f"ℹ️ Solicitud de n8n para /n8n/select-calendar-event-by-index con índice: {selected_index}")
    try:
        result = select_calendar_event_by_index(selected_index=selected_index)
        if "error" in result:
            logger.error(f"❌ Error al seleccionar evento por índice: {result['error']}")
            return {"status": "ERROR", "message": result["error"]}
        return {"status": "SUCCESS", "message": result["message"], "event_id": result.get("event_id")}
    except Exception as e:
        logger.error(f"❌ Error en endpoint /n8n/select-calendar-event-by-index: {str(e)}", exc_info=True)
        return {"status": "ERROR_BACKEND", "message": f"Error interno del servidor: {str(e)}"}



















    

@app.on_event("startup")
def startup_event() -> None:
    """Crea carpetas de depuración y habilita modo DEBUG de nuestros módulos."""
    os.makedirs("audio", exist_ok=True)
    os.makedirs("audio_debug", exist_ok=True)

    # Activa métricas detalladas ⏱️  – pon False en producción:
    set_debug(True)

    logger.info("🚀 Backend listo, streaming STT activo.")


@app.get("/")
async def root():
    return {"message": "Backend activo, streaming STT listo."}


@app.post("/twilio-voice")
async def twilio_voice():
    """
    Endpoint que Twilio llama cuando entra una llamada.
    Responde con TwiML que abre un <Stream> hacia nuestro WebSocket.
    """
    logger.info("📞 Nueva llamada entrante desde Twilio.")
    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream name="AudioStream"
            url="wss://walarcon.onrender.com/twilio-websocket" />
  </Connect>
</Response>"""
    return Response(content=twiml_response, media_type="application/xml")


@app.websocket("/twilio-websocket")
async def twilio_websocket(websocket: WebSocket):
    """
    WebSocket que recibe el audio μ-law en tiempo real desde Twilio
    y delega la lógica a TwilioWebSocketManager.
    """
    manager = TwilioWebSocketManager()
    await manager.handle_twilio_websocket(websocket)
