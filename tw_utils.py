# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets con buffering y chunking manual.
Se detecta silencio de ~0.7s para separar palabras
y un límite de 10s para no enviar audio excesivamente largo.
Luego concatenamos las transcripciones parciales en un buffer de texto.
Si detectamos silencio de ~2s, enviamos la frase completa a la IA.
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

# Límite de chunk en segundos
MAX_CHUNK_SECS = 10
# Umbral para chunk en bytes (8k mu-law => 8000 bytes por segundo)
MAX_CHUNK_BYTES = MAX_CHUNK_SECS * 8000

# Umbral de silencio para "cerrar chunk" (ej: 0.7s)
CHUNK_SILENCE_THRESHOLD = 0.7

# Umbral para "fin de frase" = 2.0s
# Si pasa 2s sin audio, consideramos que el usuario terminó su turno de habla.
END_OF_SPEECH_THRESHOLD = 2.0

async def process_full_utterance(websocket: WebSocket, conversation_history: list, partial_transcript: str, stream_sid: str):
    """
    Envía el texto completo a la IA y envía el audio de respuesta al usuario.
    Luego limpia partial_transcript.
    """
    if not partial_transcript.strip():
        return ""

    logger.info(f"[Conversation] Usuario dice: {partial_transcript}")
    conversation_history.append({"role": "user", "content": partial_transcript})
    # Llamamos a la IA
    start_ai = time.perf_counter()
    ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
    end_ai = time.perf_counter()
    logger.info(f"[Conversation] Tiempo IA: {end_ai - start_ai:.3f}s")

    # TTS
    start_tts = time.perf_counter()
    audio_response = await asyncio.to_thread(text_to_speech, ai_response)
    end_tts = time.perf_counter()
    logger.info(f"[Conversation] Tiempo TTS: {end_tts - start_tts:.3f}s")

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
            logger.error(f"[Conversation] Error enviando respuesta por websocket: {e}")
    conversation_history.append({"role": "assistant", "content": ai_response})
    return ""

async def process_chunk(audio_buffer: bytearray) -> str:
    """
    Procesa un chunk de audio (convierte a bytes) y lo transcribe con speech_to_text.
    Retorna la transcripción parcial (puede ser un fragmento de frase).
    """
    start_proc = time.perf_counter()
    audio_bytes = bytes(audio_buffer)
    audio_buffer.clear()
    partial_text = await asyncio.to_thread(speech_to_text, audio_bytes)
    end_proc = time.perf_counter()
    logger.info(f"[Chunk] Tiempo chunk: {end_proc - start_proc:.3f}s => \"{partial_text}\"")
    return partial_text

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    stream_sid = None

    # El buffer de bytes se usará para cada chunk que no exceda 10s.
    audio_buffer = bytearray()
    # El partial_transcript va concatenando resultados de chunk
    partial_transcript = ""

    # Tiempos de última recepción, para chunk y para frase final
    last_audio_time = time.perf_counter()      # actualiza con cada media
    last_speech_time = time.perf_counter()     # sirve para detectar fin del turno (2s)
    
    greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
    try:
        await websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0"})
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("[WebSocket] Usuario colgó")
                break
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"[WebSocket] Inicio de stream - SID: {stream_sid}")

                # Enviamos saludo
                audio_greeting = await asyncio.to_thread(text_to_speech, greeting)
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
                        logger.error(f"[WebSocket] Error enviando saludo: {e}")
                conversation_history.append({"role": "assistant", "content": greeting})

            elif event_type == "media" and stream_sid:
                audio_payload = data.get("media", {}).get("payload", "")
                audio_chunk = base64.b64decode(audio_payload)
                audio_buffer.extend(audio_chunk)
                now = time.perf_counter()
                
                # Evaluamos si superamos chunk max (10s)
                if len(audio_buffer) >= MAX_CHUNK_BYTES:
                    # Procesar chunk parcial
                    chunk_text = await process_chunk(audio_buffer)
                    # Concatenar al partial_transcript
                    partial_transcript += (" " + chunk_text).strip()
                    # Actualizar last_audio_time
                    last_audio_time = now
                    last_speech_time = now

                else:
                    # No se ha llegado al límite, pero puede que haya silencio
                    elapsed = now - last_audio_time
                    if elapsed > CHUNK_SILENCE_THRESHOLD:
                        # Significa que pasó ~0.7s sin audio => Cerrar chunk
                        chunk_text = await process_chunk(audio_buffer)
                        partial_transcript += (" " + chunk_text).strip()
                        last_audio_time = now
                        last_speech_time = now
                    else:
                        # Todavía no se cierra este chunk
                        last_audio_time = now

                # Checamos si hay silencio total => final de frase (2s)
                # Ej: si en 2s no recibimos más data => procesar partial_transcript entero
                if (now - last_speech_time) > END_OF_SPEECH_THRESHOLD and partial_transcript.strip():
                    # El usuario completó su turno. Mandar a la IA
                    partial_transcript = await process_full_utterance(websocket, conversation_history, partial_transcript, stream_sid)
                    # partial_transcript se vacía dentro de la función

            elif event_type == "stop":
                logger.info("[WebSocket] Llamada finalizada.")
                # Procesar cualquier chunk pendiente
                if len(audio_buffer) > 0:
                    chunk_text = await process_chunk(audio_buffer)
                    partial_transcript += (" " + chunk_text).strip()
                    audio_buffer.clear()
                # Enviar lo que se tenga en partial_transcript como frase final
                if partial_transcript.strip():
                    partial_transcript = await process_full_utterance(websocket, conversation_history, partial_transcript, stream_sid)
                break

    except Exception as e:
        logger.error(f"[WebSocket] Error general: {e}")
    finally:
        await websocket.close()
