# -*- coding: utf-8 -*-
"""
Módulo de integración Twilio con manejo de audios locales, tiempos de espera
del usuario y tiempos de espera del sistema (IA/ELabs).
"""
from datetime import datetime
import json
import logging
import base64
import asyncio
import time
import os
from pathlib import Path
from fastapi import WebSocket, WebSocketDisconnect
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================
# 🔧 CONFIGURACIÓN PRINCIPAL
# ==================================================
AUDIO_DIR = Path(__file__).parent / "audio"
DEBUG_DIR = Path(__file__).parent / "audio_debug"
DEBUG_DIR.mkdir(exist_ok=True)

# Tiempos de inactividad
USER_SILENCE_1 = 15
USER_SILENCE_2 = 25
USER_SILENCE_3 = 30

# Tiempos de retardo del sistema
SYS_WAIT_1 = 3
SYS_WAIT_2 = 7
SYS_WAIT_3 = 12
SYS_WAIT_4 = 16
SYS_ERR = 20
SYS_ENDCALL = 25

async def play_backup_audio(websocket: WebSocket, stream_sid: str, filename: str):
    """Envía un archivo de audio WAV en Base64 para Twilio."""
    if not stream_sid:
        logger.warning(f"Audio '{filename}' no se reproduce: stream_sid es None")
        return

    try:
        with open(AUDIO_DIR / filename, "rb") as f:
            audio_data = f.read()
        await websocket.send_text(json.dumps({
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio_data).decode("utf-8")}
        }))
        logger.info(f"Reproduciendo: {filename}")
    except Exception as e:
        logger.error(f"Error cargando {filename}: {str(e)}")

async def system_wait_tracker(start_time: float, websocket: WebSocket, stream_sid: str, stop_event: asyncio.Event):
    """Controla los tiempos de espera del sistema."""
    played_stages = set()
    while True:
        await asyncio.sleep(1)
        if stop_event.is_set():
            return

        elapsed = time.time() - start_time

        if elapsed >= SYS_ENDCALL:
            logger.error("Forzando terminación por demora (25s)")
            await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
            await asyncio.sleep(1)
            await websocket.close()
            return

        elif elapsed >= SYS_ERR and "SYS_ERR" not in played_stages:
            played_stages.add("SYS_ERR")
            logger.error("Reproduciendo error (20s)")
            await play_backup_audio(websocket, stream_sid, "error_sistema.wav")

        elif elapsed >= SYS_WAIT_4 and 4 not in played_stages:
            played_stages.add(4)
            await play_backup_audio(websocket, stream_sid, "espera_4.wav")

        elif elapsed >= SYS_WAIT_3 and 3 not in played_stages:
            played_stages.add(3)
            await play_backup_audio(websocket, stream_sid, "espera_3.wav")

        elif elapsed >= SYS_WAIT_2 and 2 not in played_stages:
            played_stages.add(2)
            await play_backup_audio(websocket, stream_sid, "espera_2.wav")

        elif elapsed >= SYS_WAIT_1 and 1 not in played_stages:
            played_stages.add(1)
            await play_backup_audio(websocket, stream_sid, "espera_1.wav")

async def process_full_utterance(websocket: WebSocket, conversation_history: list, partial_transcript: str, stream_sid: str):
    """Procesa la respuesta de la IA."""
    logger.info(f"[Conversación] Usuario: {partial_transcript}")

    stop_event = asyncio.Event()
    start_time = time.time()
    tracker_task = asyncio.create_task(system_wait_tracker(start_time, websocket, stream_sid, stop_event))

    try:
        ai_response = await asyncio.wait_for(
            asyncio.to_thread(
                generate_openai_response,
                conversation_history + [{"role": "user", "content": partial_transcript}]
            ),
            timeout=28
        )
        stop_event.set()
        logger.info(f"IA respondió: {ai_response}")

        audio_response = await asyncio.to_thread(text_to_speech, ai_response)
        
        # Enviar audio en chunks
        chunk_size = 8000
        for i in range(0, len(audio_response), chunk_size):
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(audio_response[i:i+chunk_size]).decode("utf-8")}
            }))

        conversation_history.extend([
            {"role": "user", "content": partial_transcript},
            {"role": "assistant", "content": ai_response}
        ])

    except asyncio.TimeoutError:
        logger.error("Timeout IA")
        stop_event.set()
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
        await websocket.close()
    finally:
        if not tracker_task.done():
            tracker_task.cancel()

async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list, stream_sid: str, audio_buffer: bytearray, last_user_activity: list):
    """Procesa el audio entrante con depuración."""
    media = data.get("media", {})
    chunk = base64.b64decode(media.get("payload", ""))
    
    # Guardar audio crudo
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    debug_path = DEBUG_DIR / f"twilio_raw_{timestamp}.ulaw"
    with open(debug_path, "wb") as f:
        f.write(chunk)
    logger.info(f"🔧 Audio guardado: {debug_path}")

    audio_buffer.extend(chunk)
    
    if len(audio_buffer) >= 2400:
        transcript = speech_to_text(bytes(audio_buffer))
        audio_buffer.clear()

        if transcript:
            # Actualizar actividad SOLO si hay transcripción
            last_user_activity[0] = time.time()
            logger.info(f"🛎️ Actividad detectada: {transcript}")
            await process_full_utterance(websocket, conversation_history, transcript, stream_sid)
        else:
            logger.info("🔇 Audio sin transcripción")

async def process_farewell_ai(websocket: WebSocket, conversation_history: list, stream_sid: str):
    """
    Pide a la IA generar una despedida breve y reproduce el audio resultante.
    """
    try:
        system_msg = (
            "El usuario ha estado en silencio 30 segundos. "
            "Despídete brevemente (menos de 30 palabras) y cierra la llamada."
        )
        ai_farewell = await asyncio.wait_for(
            asyncio.to_thread(
                generate_openai_response,
                conversation_history + [{"role": "system", "content": system_msg}]
            ),
            timeout=8
        )
        audio_farewell = await asyncio.to_thread(text_to_speech, ai_farewell)

        # Enviamos en chunks
        chunk_size = 8000
        for i in range(0, len(audio_farewell), chunk_size):
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_farewell[i:i+chunk_size]).decode("utf-8")
                }
            }))

        conversation_history.append({"role": "assistant", "content": ai_farewell})

    except asyncio.TimeoutError:
        logger.error("La IA no pudo generar la despedida final a tiempo.")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
    except Exception as e:
        logger.error(f"Error generando la despedida de la IA: {e}")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")

async def handle_twilio_websocket(websocket: WebSocket):
    """Maneja la conexión WebSocket."""
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None
    last_user_activity = [time.time()]  # Usar lista para modificación por referencia
    user_warn_stage = 0

    try:
        while True:
            message = await asyncio.wait_for(websocket.receive_text(), timeout=35)
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"🔄 Nuevo stream: {stream_sid}")
                await play_backup_audio(websocket, stream_sid, "saludo.wav")

            elif event_type == "media":
                await process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer, last_user_activity)

            # Verificar inactividad
            elapsed_silence = time.time() - last_user_activity[0]

            if elapsed_silence >= USER_SILENCE_1 and user_warn_stage == 0:
                user_warn_stage = 1
                await play_backup_audio(websocket, stream_sid, "noescucho_1.wav")

            elif elapsed_silence >= USER_SILENCE_2 and user_warn_stage == 1:
                user_warn_stage = 2
                await play_backup_audio(websocket, stream_sid, "noescucho_1.wav")

            elif elapsed_silence >= USER_SILENCE_3:
                logger.info("⏰ 30s de silencio")
                await process_farewell_ai(websocket, conversation_history, stream_sid)
                await websocket.close()
                break

    except (WebSocketDisconnect, asyncio.TimeoutError):
        logger.info("❌ Conexión cerrada")
    except Exception as e:
        logger.error(f"💥 Error: {str(e)}")
        if stream_sid:
            await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
    finally:
        await websocket.close()
