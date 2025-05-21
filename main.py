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
