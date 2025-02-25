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
        # Dividir el audio en fragmentos de 8000 bytes (1 segundo cada uno)
        chunk_size = 8000
        for i in range(0, len(audio_response), chunk_size):
            chunk = audio_response[i:i+chunk_size]
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(chunk).decode("utf-8")
                }
            }
            try:
                await websocket.send_text(json.dumps(media_message))
                logger.info(f"[Audio] Enviado chunk {i//chunk_size + 1}")
            except Exception as e:
                logger.error(f"[Conversation] Error al enviar TTS: {e}")
                break

        # Enviar marcador de fin de audio
        try:
            await websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "end_of_audio"}
            }))
        except Exception as e:
            logger.error(f"[Audio] Error enviando mark: {e}")

    return ""  # limpiamos partial_transcript



async def process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer):
    media = data.get("media", {})
    chunk = base64.b64decode(media.get("payload", ""))
    audio_buffer.extend(chunk)
    
    if len(audio_buffer) >= MAX_CHUNK_BYTES:
        await process_full_utterance(
            websocket=websocket,
            conversation_history=conversation_history,
            partial_transcript=speech_to_text(bytes(audio_buffer)),
            stream_sid=stream_sid
        )
        audio_buffer.clear()

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None
    last_activity = time.time()

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
                last_activity = time.time()
                await process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer)

            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                break

            # Timeout por inactividad
            if time.time() - last_activity > 15:
                logger.warning("🕒 Timeout por inactividad")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    except Exception as e:
        logger.error(f"💥 Error crítico: {str(e)}")
    finally:
        await websocket.close()
