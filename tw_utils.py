# -*- coding: utf-8 -*-
"""
Módulo de integración Twilio con chunking manual.
- Máx 10s
- 0.5s => cierra chunk
- 1.0s => envía frase a IA
- Filtra si <300ms => skip
- Manejo de errores al cerrar websocket
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

MAX_CHUNK_SECS = 10
MAX_CHUNK_BYTES = MAX_CHUNK_SECS * 8000  # 10s * 8000 B/s
CHUNK_SILENCE_THRESHOLD = 0.5  # 0.5s para cerrar chunk
END_OF_SPEECH_THRESHOLD = 1.0   # 1.0s => se manda a IA

async def process_full_utterance(websocket: WebSocket, conversation_history: list, partial_transcript: str, stream_sid: str):
    if not partial_transcript.strip():
        return ""
    logger.info(f"[Conversation] Usuario dice: {partial_transcript}")

    # Llamamos a la IA
    start_ai = time.perf_counter()
    ai_response = await asyncio.to_thread(
        generate_openai_response,
        conversation_history + [{"role": "user", "content": partial_transcript}]
    )
    end_ai = time.perf_counter()
    logger.info(f"[Conversation] Tiempo IA: {end_ai - start_ai:.3f}s")
    logger.info(f"[Conversation] Respuesta IA: {ai_response}")

    # TTS
    start_tts = time.perf_counter()
    audio_response = await asyncio.to_thread(text_to_speech, ai_response)
    end_tts = time.perf_counter()
    logger.info(f"[Conversation] Tiempo TTS: {end_tts - start_tts:.3f}s")

    conversation_history.append({"role": "user", "content": partial_transcript})
    conversation_history.append({"role": "assistant", "content": ai_response})

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
            logger.error(f"[Conversation] Error al enviar TTS: {e}")

    return ""  # limpiamos partial_transcript

async def process_chunk(audio_buffer: bytearray) -> str:
    """
    Procesa el chunk actual con speech_to_text y retorna parcial.
    """
    if not audio_buffer:
        return ""
    start_proc = time.perf_counter()
    chunk_bytes = bytes(audio_buffer)
    audio_buffer.clear()

    partial = await asyncio.to_thread(speech_to_text, chunk_bytes)
    end_proc = time.perf_counter()
    logger.info(f"[Chunk] Parcial='{partial}' => {end_proc - start_proc:.3f}s")
    return partial

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    stream_sid = None

    audio_buffer = bytearray()
    partial_transcript = ""

    last_audio_time = time.perf_counter()
    last_speech_time = time.perf_counter()

    try:
        await websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0"})

        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("[WebSocket] El usuario colgó.")
                break

            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"[WebSocket] Inicia stream - SID: {stream_sid}")

                greeting = "Hola! Consultorio del Dr. Alarcón, ¿en qué puedo ayudarle?"
                audio_greet = await asyncio.to_thread(text_to_speech, greeting)
                if audio_greet:
                    try:
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": base64.b64encode(audio_greet).decode("utf-8")
                            }
                        }))
                    except Exception as e:
                        logger.error(f"[WebSocket] Error enviando saludo: {e}")
                conversation_history.append({"role": "assistant", "content": greeting})

            elif event_type == "media" and stream_sid:
                audio_payload = data.get("media", {}).get("payload", "")
                if not audio_payload:
                    continue

                audio_chunk = base64.b64decode(audio_payload)
                audio_buffer.extend(audio_chunk)
                now = time.perf_counter()

                # Límite de 10s
                if len(audio_buffer) >= MAX_CHUNK_BYTES:
                    logger.info("[Chunk] Se excedieron 10s => procesamos chunk.")
                    chunk_text = await process_chunk(audio_buffer)
                    if chunk_text:
                        partial_transcript = (partial_transcript + " " + chunk_text).strip()
                    last_audio_time = now
                    last_speech_time = now

                else:
                    elapsed_chunk = now - last_audio_time
                    if elapsed_chunk > CHUNK_SILENCE_THRESHOLD:
                        # Cierra chunk
                        chunk_text = await process_chunk(audio_buffer)
                        if chunk_text:
                            partial_transcript = (partial_transcript + " " + chunk_text).strip()
                        last_audio_time = now
                        last_speech_time = now
                    else:
                        last_audio_time = now

                # Fin de frase
                if (now - last_speech_time) > END_OF_SPEECH_THRESHOLD and partial_transcript.strip():
                    logger.info("[Frase] Usuario terminó => enviamos a IA.")
                    partial_transcript = await process_full_utterance(
                        websocket, conversation_history, partial_transcript, stream_sid
                    )

            elif event_type == "stop":
                logger.info("[WebSocket] Llamada finalizada.")
                if audio_buffer:
                    chunk_text = await process_chunk(audio_buffer)
                    if chunk_text:
                        partial_transcript = (partial_transcript + " " + chunk_text).strip()

                if partial_transcript.strip():
                    partial_transcript = await process_full_utterance(
                        websocket, conversation_history, partial_transcript, stream_sid
                    )
                break

    except Exception as e:
        logger.error(f"[WebSocket] Error general: {e}")
    finally:
        # Evitar error Unexpected ASGI message ...
        try:
            await websocket.close()
        except Exception as e:
            logger.warning(f"[WebSocket] Error al cerrar: {e}")
