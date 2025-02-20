# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets con buffering y VAD.
"""

import json
import logging
import base64
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)






async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list, stream_sid: str, audio_buffer: bytearray):
    try:
        audio_payload = data.get("media", {}).get("payload", "")
        audio_chunk = base64.b64decode(audio_payload)
        audio_buffer.extend(audio_chunk)

        # Procesa solo cuando hay suficiente audio (1-2 segundos)
        if len(audio_buffer) >= 1600 * 10:
            transcribed_text = await asyncio.to_thread(speech_to_text, bytes(audio_buffer))
            audio_buffer.clear()

            if not transcribed_text:
                return

            logger.info(f"👤 Usuario: {transcribed_text}")
            conversation_history.append({"role": "user", "content": transcribed_text})

            # Generar respuesta en el mismo idioma detectado
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
        logger.error(f"❌ Error en process_audio_stream: {str(e)}")








async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None

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
                await process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer)

            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    finally:
        await websocket.close()
