# main.py
# -*- coding: utf-8 -*-
"""
🚀 PUNTO DE ENTRADA PRINCIPAL
==============================
FastAPI app que maneja:
- Llamadas de voz vía Twilio WebSocket
- Mensajes de texto vía webhook
- APIs REST para herramientas

Usa la nueva arquitectura modular en lugar del monolítico tw_utils.
"""

import os
import logging
import json
import traceback
from typing import Optional, Union, List, Dict, Any
from fastapi import FastAPI, Response, WebSocket, Body, Request
from pydantic import BaseModel
import time

# === NUEVA ARQUITECTURA ===
from call_orchestrator import CallOrchestrator

# === MÓDULOS EXISTENTES ===
from consultarinfo import router as consultorio_router
from aiagent_text import process_text_message
import buscarslot
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from selectevent import select_calendar_event_by_index
from utils import search_calendar_event_by_phone

# ===== CONFIGURACIÓN DE LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)5s | %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)

logger = logging.getLogger(__name__)

# Silenciar logs verbosos
for noisy_module in [
    "httpcore.http11",
    "httpcore.connection", 
    "httpx",
    "websockets.client",
    "websockets.server",
    "urllib3.connectionpool",
    "asyncio",
    "uvicorn.access",
    "deepgram.clients.common.v1.abstract_async_websocket",
    "twilio.http_client",
]:
    logging.getLogger(noisy_module).setLevel(logging.WARNING)

# ===== FASTAPI APP =====
app = FastAPI(title="AI Assistant Backend")

# ===== ESTADO GLOBAL =====
conversation_histories: Dict[str, List[Dict]] = {}

# ===== RUTAS =====
app.include_router(consultorio_router, prefix="/api_v1")


@app.on_event("startup")
async def startup_event():
    """
    🚀 Inicialización al arrancar el servidor
    """
    t0 = time.perf_counter()
    # Crear directorios necesarios
    os.makedirs("audio", exist_ok=True)
    os.makedirs("audio_debug", exist_ok=True)
    
    # Pre-cargar datos en caché
    try:
        buscarslot.load_free_slots_to_cache()
    except Exception as e:
        logger.warning(f"Error pre-cargando datos: {e}")
    
    logger.info("🚀 Backend iniciado - Nueva arquitectura modular activa")
    logger.info(f"[LATENCIA] Backend startup completado en {1000*(time.perf_counter()-t0):.1f} ms")


@app.get("/")
async def root():
    """Endpoint de salud básico"""
    return {
        "message": "AI Assistant Backend activo",
        "version": "2.0",
        "architecture": "modular"
    }


# ========== ENDPOINTS DE VOZ (TWILIO) ==========

@app.post("/twilio-voice")
async def twilio_voice():
    """
    📞 Endpoint que Twilio llama cuando entra una llamada
    
    Responde con TwiML que abre un Stream hacia nuestro WebSocket
    """
    logger.info("[FUNCIONALIDAD] Nueva llamada entrante (POST /twilio-voice)")
    t0 = time.perf_counter()
    
    # TwiML para iniciar streaming
    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream name="AudioStream" url="wss://walarcon.onrender.com/twilio-websocket" track="inbound_track">
      <Parameter name="content-type" value="audio/x-mulaw;rate=8000;channels=1"/>
    </Stream>
  </Connect>
</Response>"""
    
    logger.info(f"[LATENCIA] Respuesta TwiML generada en {1000*(time.perf_counter()-t0):.1f} ms")
    return Response(content=twiml_response, media_type="application/xml")


@app.websocket("/twilio-websocket")
async def twilio_websocket(websocket: WebSocket):
    """
    🎙️ WebSocket que recibe audio en tiempo real de Twilio
    
    Usa el nuevo CallOrchestrator en lugar del viejo TwilioWebSocketManager
    """
    logger.info("[FUNCIONALIDAD] WebSocket /twilio-websocket aceptado")
    t0 = time.perf_counter()
    orchestrator = CallOrchestrator()
    await orchestrator.handle_call(websocket)
    logger.info(f"[LATENCIA] WebSocket /twilio-websocket completado en {1000*(time.perf_counter()-t0):.1f} ms")


# ========== ENDPOINTS DE TEXTO (WEBHOOKS) ==========

class N8NMessage(BaseModel):
    """Modelo para mensajes entrantes de n8n"""
    platform: Optional[str] = None
    user_id: Optional[str] = None
    conversation_id: Optional[str] = None
    user_name: Optional[str] = None
    phone: Optional[str] = None
    message_text: Optional[str] = None


@app.post("/webhook/n8n_message")
async def receive_n8n_message(message_data: N8NMessage):
    """
    💬 Webhook para mensajes de texto desde n8n
    
    Procesa mensajes de WhatsApp, Instagram, etc.
    """
    logger.info(f"[FUNCIONALIDAD] Mensaje de {message_data.user_id}: '{message_data.message_text}' (POST /webhook/n8n_message)")
    t0 = time.perf_counter()
    
    user_id = message_data.user_id or "unknown_user"
    conversation_id = message_data.conversation_id or user_id
    current_message = message_data.message_text or ""
    
    # Gestionar historial
    if conversation_id not in conversation_histories:
        conversation_histories[conversation_id] = []
    
    # Limitar historial a 20 mensajes
    if len(conversation_histories[conversation_id]) > 20:
        conversation_histories[conversation_id] = conversation_histories[conversation_id][-20:]
    
    history = conversation_histories[conversation_id]
    history.append({"role": "user", "content": current_message})
    
    # Procesar con IA de texto
    try:
        response_data = process_text_message(
            user_id=user_id,
            current_user_message=current_message,
            history=history
        )
        
        ai_reply = response_data.get("reply_text", "No pude obtener una respuesta.")
        status = response_data.get("status", "success")
        
    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}")
        traceback.print_exc()
        ai_reply = "Hubo un error procesando tu mensaje. Por favor intenta de nuevo."
        status = "error"
    
    # Agregar respuesta al historial
    if ai_reply:
        history.append({"role": "assistant", "content": ai_reply})
    
    logger.info(f"[LATENCIA] Mensaje de texto procesado en {1000*(time.perf_counter()-t0):.1f} ms")
    return {"reply_text": ai_reply, "status": status}


# ========== APIS DE HERRAMIENTAS ==========

@app.post("/n8n/process-appointment-request")
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
    """🗓️ Procesa solicitud de cita"""
    logger.info(f"🗓️ Procesando solicitud: {user_query_for_date_time}")
    
    try:
        result = buscarslot.process_appointment_request(
            user_query_for_date_time=user_query_for_date_time,
            day_param=day_param,
            month_param=month_param,
            year_param=year_param,
            fixed_weekday_param=fixed_weekday_param,
            explicit_time_preference_param=explicit_time_preference_param,
            is_urgent_param=is_urgent_param or False,
            more_late_param=more_late_param or False,
            more_early_param=more_early_param or False
        )
        return result
    except Exception as e:
        logger.error(f"Error en appointment request: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


@app.post("/n8n/create-calendar-event")
async def n8n_create_calendar_event(
    name: str = Body(...),
    phone: str = Body(...),
    reason: str = Body(...),
    start_time: str = Body(...),
    end_time: str = Body(...)
):
    """📅 Crea evento en calendario"""
    logger.info(f"📅 Creando cita para {name}")
    
    try:
        result = create_calendar_event(
            name=name,
            phone=phone,
            reason=reason,
            start_time=start_time,
            end_time=end_time
        )
        
        if "error" in result:
            return {"status": "ERROR", "message": result["error"]}
            
        return {
            "status": "SUCCESS",
            "event_id": result.get("id"),
            "message": "Cita creada exitosamente"
        }
    except Exception as e:
        logger.error(f"Error creando cita: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


@app.post("/n8n/edit-calendar-event")
async def n8n_edit_calendar_event(
    event_id: str = Body(...),
    new_start_time_iso: str = Body(...),
    new_end_time_iso: str = Body(...),
    new_name: Optional[str] = Body(None),
    new_reason: Optional[str] = Body(None),
    new_phone_for_description: Optional[str] = Body(None)
):
    """✏️ Edita evento existente"""
    logger.info(f"✏️ Editando evento {event_id}")
    
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
            return {"status": "ERROR", "message": result["error"]}
            
        return {
            "status": "SUCCESS",
            "message": "Cita modificada exitosamente"
        }
    except Exception as e:
        logger.error(f"Error editando cita: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


@app.post("/n8n/delete-calendar-event")
async def n8n_delete_calendar_event(
    event_id: str = Body(...),
    original_start_time_iso: str = Body(...)
):
    """🗑️ Elimina evento del calendario"""
    logger.info(f"🗑️ Eliminando evento {event_id}")
    
    try:
        result = delete_calendar_event(
            event_id=event_id,
            original_start_time_iso=original_start_time_iso
        )
        
        if "error" in result:
            return {"status": "ERROR", "message": result["error"]}
            
        return {
            "status": "SUCCESS",
            "message": "Cita eliminada exitosamente"
        }
    except Exception as e:
        logger.error(f"Error eliminando cita: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


@app.post("/n8n/search-calendar-event-by-phone")
async def n8n_search_calendar_event_by_phone(phone: str = Body(...)):
    """🔍 Busca citas por teléfono"""
    logger.info(f"🔍 Buscando citas para teléfono: {phone}")
    
    try:
        search_results = search_calendar_event_by_phone(phone=phone)
        return {"search_results": search_results}
    except Exception as e:
        logger.error(f"Error buscando citas: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


@app.post("/n8n/select-calendar-event-by-index")
async def n8n_select_calendar_event_by_index(selected_index: int = Body(...)):
    """👆 Selecciona cita por índice"""
    logger.info(f"👆 Seleccionando cita índice: {selected_index}")
    
    try:
        result = select_calendar_event_by_index(selected_index=selected_index)
        
        if "error" in result:
            return {"status": "ERROR", "message": result["error"]}
            
        return {
            "status": "SUCCESS",
            "message": result["message"],
            "event_id": result.get("event_id")
        }
    except Exception as e:
        logger.error(f"Error seleccionando cita: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}


# ========== ENDPOINTS DE ADMINISTRACIÓN ==========

@app.get("/admin/call-status")
async def get_call_status():
    """
    📊 Obtiene el estado de las llamadas activas
    
    Útil para debugging y monitoreo
    """
    t0 = time.perf_counter()
    # TODO: Implementar tracking de llamadas activas
    logger.info(f"[LATENCIA] Admin call-status consultado en {1000*(time.perf_counter()-t0):.1f} ms")
    return {
        "active_calls": 0,
        "message": "Endpoint en desarrollo"
    }


@app.post("/admin/reload-cache")
async def reload_cache():
    """
    🔄 Recarga los datos en caché
    
    Útil cuando se actualizan horarios o información
    """
    t0 = time.perf_counter()
    try:
        buscarslot.load_free_slots_to_cache()
        
        logger.info(f"[LATENCIA] Admin reload-cache completado en {1000*(time.perf_counter()-t0):.1f} ms")
        return {
            "status": "SUCCESS",
            "message": "Caché recargado exitosamente"
        }
    except Exception as e:
        logger.error(f"Error recargando caché: {e}")
        return {
            "status": "ERROR",
            "message": str(e)
        }


@app.get("/admin/health-check")
async def health_check():
    """
    🏥 Endpoint detallado de salud
    
    Verifica el estado de todos los componentes
    """
    t0 = time.perf_counter()
    health_status = {
        "status": "healthy",
        "components": {
            "api": "healthy",
            "cache": "unknown",
            "integrations": "unknown"
        }
    }
    
    # Verificar caché
    try:
        # No hay caché de datos de consultorio, solo slots de calendario
        health_status["components"]["cache"] = "healthy" 
    except Exception as e:
        health_status["components"]["cache"] = f"error: {str(e)}"
        health_status["status"] = "degraded"
    
    logger.info(f"[LATENCIA] Admin health-check completado en {1000*(time.perf_counter()-t0):.1f} ms")
    return health_status


# ========== MANEJO DE ERRORES GLOBAL ==========

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    🚨 Manejador global de excepciones
    """
    logger.error(f"Error no manejado: {exc}", exc_info=True)
    
    return {
        "status": "ERROR",
        "message": "Error interno del servidor",
        "detail": str(exc) if os.getenv("DEBUG") == "true" else None
    }


if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        reload=os.getenv("RELOAD", "false").lower() == "true"
    )