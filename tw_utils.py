# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets con buffering y detección de silencio.
"""

import json
import logging
import base64
import asyncio
import time
from fastapi import WebSocket, WebSocketDisconnect
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def process_audio_buffer(websocket: WebSocket, conversation_history: list, stream_sid: str, audio_buffer: bytearray):
    """
    Procesa el audio acumulado en el buffer, lo transcribe y envía la respuesta.
    """
    try:
        # Convierte el bytearray a bytes
        transcribed_text = await asyncio.to_thread(speech_to_text, bytes(audio_buffer))
        # Limpiamos el buffer
        audio_buffer.clear()
        if not transcribed_text:
            return

        logger.info(f"👤 Usuario: {transcribed_text}")
        conversation_history.append({"role": "user", "content": transcribed_text})

        # Generar respuesta de la IA
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
        audio_response = await asyncio.to_thread(text_to_speech, ai_response)

        if audio_response:
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_response).decode("utf-8")
                }
            }
            await websocket.send_text(json.dumps(media_message))
            conversation_history.append({"role": "assistant", "content": ai_response})

    except Exception as e:
        logger.error(f"❌ Error en process_audio_buffer: {str(e)}")

async def check_buffer_periodically(websocket: WebSocket, conversation_history: list, stream_sid: str, audio_buffer: bytearray, last_media_time: list):
    """
    Tarea en segundo plano que revisa el buffer de audio periódicamente.
    Si ha pasado más de 1 segundo desde el último audio recibido, procesa el buffer.
    """
    while True:
        await asyncio.sleep(0.3)
        current_time = asyncio.get_event_loop().time()
        # Si hay audio acumulado y han pasado más de 1 segundo de inactividad
        if audio_buffer and (current_time - last_media_time[0]) > 1.0:
            await process_audio_buffer(websocket, conversation_history, stream_sid, audio_buffer)

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None
    # Usamos una lista para poder actualizar el valor dentro de la tarea en background.
    last_media_time = [asyncio.get_event_loop().time()]

    # Iniciar tarea en segundo plano para revisar el buffer
    asyncio.create_task(check_buffer_periodically(websocket, conversation_history, stream_sid, audio_buffer, last_media_time))

    try:
        await websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0"})
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"🎤 Inicio de stream - SID: {stream_sid}")

                greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
                audio_greeting = await asyncio.to_thread(text_to_speech, greeting)

                if audio_greeting:
                    await websocket.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": base64.b64encode(audio_greeting).decode("utf-8")
                        }
                    }))
                conversation_history.append({"role": "assistant", "content": greeting})

            elif event_type == "media" and stream_sid:
                # Acumula el audio recibido
                audio_payload = data.get("media", {}).get("payload", "")
                audio_chunk = base64.b64decode(audio_payload)
                audio_buffer.extend(audio_chunk)
                # Actualiza la marca de tiempo del último audio recibido
                last_media_time[0] = asyncio.get_event_loop().time()

            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                # Si hay audio pendiente, procesarlo antes de cerrar.
                if audio_buffer:
                    await process_audio_buffer(websocket, conversation_history, stream_sid, audio_buffer)
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    finally:
        await websocket.close()
