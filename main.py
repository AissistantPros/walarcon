# main.py
import os
import logging
from fastapi import FastAPI, Response, WebSocket, Body, Request
import fastapi
from aiagent import generate_openai_response_main
from tw_utils import TwilioWebSocketManager, set_debug   
from consultarinfo import get_consultorio_data_from_cache, load_consultorio_data_to_cache 
from consultarinfo import router as consultorio_router 
import buscarslot       
from typing import Optional, Union, List 
from crearcita import create_calendar_event 
from editarcita import edit_calendar_event   
from eliminarcita import delete_calendar_event 
from selectevent import select_calendar_event_by_index
from utils import search_calendar_event_by_phone 
from pydantic import BaseModel, Field
from aiagent_text import process_text_message
import traceback
import json


# ───────── CONFIGURACIÓN DE LOGGING ────────────────────────────
import fastapi.logger as fastapi_logger

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
                    datefmt="%H:%M:%S")

# 🔧 Parche para Deepgram: asegurar que fastapi.logger tenga los métodos info/warning/etc
_base = logging.getLogger("fastapi")
for lvl in ("info", "warning", "error", "debug"):
    if not hasattr(fastapi_logger, lvl):
        setattr(fastapi_logger, lvl, getattr(_base, lvl))

logger = logging.getLogger(__name__)

# Silenciamos verbosidad de librerías externas
for noisy in (
    "openai._base_client",    # peticiones HTTP a OpenAI
    "httpcore.http11",        # tráfico httpx-httpcore
    "httpcore.connection",
    "httpx",
    "websockets.client",
    "websockets.server",
    "urllib3.connectionpool",
    "asyncio",
    "uvicorn.access",
    "uvicorn.error",
    "deepgram.clients.common.v1.abstract_async_websocket",
    "fastapi",
    "twilio.http_client",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)



# ───────── FASTAPI ─────────────────────────────────────────────
app = FastAPI()
conversation_histories = {} # Diccionario para almacenar historiales de conversación por usuario

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


class N8NMessage(BaseModel):
    platform: Optional[str] = None
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    user_name: Optional[str] = None
    phone: Optional[str] = None
    message_text: Optional[str] = None

    
    
    


print("➡️ Registrando endpoint /webhook/n8n_message")
@app.post("/webhook/n8n_message")
async def receive_n8n_message(message_data: N8NMessage):
    print(f" main.py webhook: Mensaje recibido de n8n para el usuario {message_data.user_id}: '{message_data.message_text}'")

    user_id = message_data.user_id
    # Usar conversation_id si existe, sino user_id.
    # Este conversation_id también se usará para los logs en aiagent_text.py
    conversation_id = message_data.conversation_id or user_id 
    current_user_message_text = message_data.message_text

    # 1. Recuperar o inicializar el historial de esta conversación
    if conversation_id not in conversation_histories:
        # MODIFICADO: Añadir 'conversation_id_for_logs' al inicio del historial
        # para que aiagent_text.py pueda usarlo en sus logs.
        conversation_histories[conversation_id] = [{"conversation_id_for_logs": conversation_id}]
    
    current_conversation_history = conversation_histories[conversation_id]

    # 2. Añadir el mensaje actual del usuario al historial
    current_conversation_history.append({"role": "user", "content": current_user_message_text})

    print(f" main.py webhook (conversation: {conversation_id}): Historial ANTES de llamar a IA (longitud {len(current_conversation_history)}): {json.dumps(current_conversation_history, indent=2)}")

    # 3. Llamar a nuestro agente de IA para procesar el mensaje
    status_to_return = "error_unknown_default" # Valor por defecto

    try:
        # Pasar el conversation_id también a process_text_message si lo adaptaste para usarlo en logs
        # Si no, user_id es suficiente como primer parámetro para la lógica interna de process_text_message
        agent_response_data = process_text_message(
            user_id=user_id, 
            current_user_message=current_user_message_text,
            conversation_history=current_conversation_history
        )
        
        ai_reply_text = agent_response_data.get("reply_text", "No pude obtener una respuesta.")
        status_to_return = agent_response_data.get("status", "success_unknown_status_from_agent")

    except Exception as e:
        print(f" main.py webhook (conversation: {conversation_id}): ERROR al llamar a process_text_message: {str(e)}")
        # MODIFICADO: Añadir traceback.print_exc() para logs detallados del error original
        traceback.print_exc() 
        ai_reply_text = "Hubo un error interno al procesar tu mensaje. Por favor, intenta de nuevo más tarde."
        status_to_return = "error_calling_agent_exception_in_main"


    # 4. Añadir la respuesta de la IA al historial (si hubo una respuesta válida)
    if ai_reply_text: 
        current_conversation_history.append({"role": "assistant", "content": ai_reply_text})
    
    # Actualizar el historial global
    conversation_histories[conversation_id] = current_conversation_history 

    print(f" main.py webhook (conversation: {conversation_id}): Respuesta de la IA para el usuario: '{ai_reply_text}'")
    print(f" main.py webhook (conversation: {conversation_id}): Historial DESPUÉS de respuesta IA (longitud {len(current_conversation_history)}): {json.dumps(current_conversation_history, indent=2)}")


    # 5. Devolver la respuesta de la IA a n8n
    return {"reply_text": ai_reply_text, "status": status_to_return}




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
    Responde con TwiML que abre un <Stream> hacia nuestro WebSocket
    y le indica a Twilio que use audio PCM lineal (audio/raw).
    """
    logger.info("📞 Nueva llamada entrante desde Twilio.")

    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream name="AudioStream" url="wss://walarcon.onrender.com/twilio-websocket" track="inbound_track">
      <Parameter name="content-type" value="audio/x-mulaw;rate=8000;channels=1"/>
    </Stream>
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






