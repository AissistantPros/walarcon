# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets con buffering y detección de silencio.
Incluye mediciones de tiempo para evaluar la latencia y ajustes para evitar que se "cuelgue" el buffer.
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
    Procesa el audio acumulado en el buffer: lo transcribe y envía la respuesta.
    Se mide el tiempo de procesamiento.
    """
    try:
        start_proc = time.perf_counter()
        # Convertir el bytearray a bytes
        audio_bytes = bytes(audio_buffer)
        transcribed_text = await asyncio.to_thread(speech_to_text, audio_bytes)
        # Limpiar buffer
        audio_buffer.clear()
        end_proc = time.perf_counter()
        logger.info(f"Tiempo procesamiento y transcripción del buffer: {end_proc - start_proc:.3f} s")
        
        if not transcribed_text:
            return

        logger.info(f"👤 Usuario: {transcribed_text}")
        conversation_history.append({"role": "user", "content": transcribed_text})

        # Generar respuesta de la IA
        start_ai = time.perf_counter()
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
        end_ai = time.perf_counter()
        logger.info(f"Tiempo generación respuesta IA: {end_ai - start_ai:.3f} s")
        
        # Convertir respuesta a audio
        start_tts = time.perf_counter()
        audio_response = await asyncio.to_thread(text_to_speech, ai_response)
        end_tts = time.perf_counter()
        logger.info(f"Tiempo conversión TTS: {end_tts - start_tts:.3f} s")
        
        if audio_response:
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_response).decode("utf-8")
                }
            }
            try:
                await websocket.send_text(json.dumps(media_message))
            except Exception as e:
                logger.error(f"❌ Error al enviar mensaje por websocket: {e}")
            conversation_history.append({"role": "assistant", "content": ai_response})
    except Exception as e:
        logger.error(f"❌ Error en process_audio_buffer: {e}")

async def check_buffer_periodically(websocket: WebSocket, conversation_history: list, stream_sid_getter: asyncio.Future, audio_buffer: bytearray, last_media_time: list):
    """
    Tarea en segundo plano que revisa el buffer de audio periódicamente.
    Si ha pasado más de 0.7 segundos desde el último dato y hay audio acumulado, procesa el buffer.
    """
    while True:
        await asyncio.sleep(0.3)
        current_time = time.perf_counter()
        # Si hay audio acumulado y han pasado más de 0.7 segundos sin recibir nuevos datos
        if audio_buffer and (current_time - last_media_time[0]) > 0.7:
            logger.info("Detectado silencio prolongado, procesando buffer...")
            # Se obtiene el stream_sid actual (ya que éste se actualiza en el 'start')
            stream_sid = stream_sid_getter.result() if stream_sid_getter.done() else None
            if stream_sid:
                await process_audio_buffer(websocket, conversation_history, stream_sid, audio_buffer)

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    # Usamos un Future para almacenar el stream_sid una vez que se reciba
    stream_sid_future = asyncio.Future()
    last_media_time = [time.perf_counter()]

    # Inicia la tarea en segundo plano para revisar el buffer
    buffer_task = asyncio.create_task(check_buffer_periodically(websocket, conversation_history, stream_sid_future, audio_buffer, last_media_time))

    try:
        start_conn = time.perf_counter()
        await websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0"})
        end_conn = time.perf_counter()
        logger.info(f"Tiempo envío mensaje 'connected': {end_conn - start_conn:.3f} s")

        while True:
            msg_start = time.perf_counter()
            message = await websocket.receive_text()
            msg_end = time.perf_counter()
            logger.info(f"Tiempo recepción de mensaje: {msg_end - msg_start:.3f} s")
            
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                if not stream_sid_future.done():
                    stream_sid_future.set_result(stream_sid)
                logger.info(f"🎤 Inicio de stream - SID: {stream_sid}")

                greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
                start_tts = time.perf_counter()
                audio_greeting = await asyncio.to_thread(text_to_speech, greeting)
                end_tts = time.perf_counter()
                logger.info(f"Tiempo TTS para saludo: {end_tts - start_tts:.3f} s")

                if audio_greeting:
                    try:
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(audio_greeting).decode("utf-8")
                            }
                        }))
                    except Exception as e:
                        logger.error(f"❌ Error al enviar saludo: {e}")
                conversation_history.append({"role": "assistant", "content": greeting})

            elif event_type == "media" and stream_sid:
                audio_payload = data.get("media", {}).get("payload", "")
                audio_chunk = base64.b64decode(audio_payload)
                audio_buffer.extend(audio_chunk)
                last_media_time[0] = time.perf_counter()

            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                if audio_buffer:
                    await process_audio_buffer(websocket, conversation_history, stream_sid, audio_buffer)
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    except Exception as e:
        logger.error(f"❌ Error en handle_twilio_websocket: {e}")
    finally:
        buffer_task.cancel()
        try:
            await websocket.close()
        except Exception as e:
            logger.error(f"❌ Error al cerrar websocket: {e}")
