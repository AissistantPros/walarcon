# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets
Procesa llamadas en tiempo real usando Whisper (STT) y ElevenLabs (TTS).
"""

import json
import time
import logging
import base64  # ✅ Cambio crítico: Añadido para decodificar base64
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
from aiagent import generate_openai_response
from audio_utils import speech_to_text, text_to_speech

# Configuración de logging para depuración
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list):
    """
    Procesa audio en tiempo real desde Twilio, transcribe con Whisper y responde con audio.
    """
    try:
        start_time = time.time()

        # ✅ Cambio crítico: Decodificar base64 (Twilio envía payload en este formato)
        audio_payload = data.get("media", {}).get("payload", "")
        audio_bytes = base64.b64decode(audio_payload)  # Corrección aplicada

        if not audio_bytes:
            logger.warning("⚠️ No se recibió audio válido")
            return

        transcribed_text = await speech_to_text(audio_bytes)
        if not transcribed_text:
            logger.warning("⚠️ No se pudo transcribir el audio")
            return

        logger.info(f"🗣️ Usuario: {transcribed_text}")
        conversation_history.append({"role": "user", "content": transcribed_text})

        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
        
        # ✅ Cambio crítico: Manejo de errores en TTS
        audio_response = await text_to_speech(ai_response)
        if not audio_response:
            error_audio = await text_to_speech("Lo siento, hubo un error. Por favor, intente de nuevo.")
            if error_audio:
                await websocket.send_bytes(error_audio)
            return

        await websocket.send_bytes(audio_response)
        conversation_history.append({"role": "assistant", "content": ai_response})

        end_time = time.time()
        logger.info(f"⏱️ Tiempo total de procesamiento: {end_time - start_time:.2f} seg.")

    except Exception as e:
        logger.error(f"❌ Error en `process_audio_stream()`: {str(e)}")

async def handle_twilio_websocket(websocket: WebSocket):
    """
    Maneja la conexión WebSocket de Twilio.
    """
    await websocket.accept()
    conversation_history = []
    call_start_time = time.time()

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "connected":
                logger.info("✅ Conexión WebSocket establecida con Twilio")

            elif event_type == "start":
                logger.info(f"🎤 Inicio de stream - Call SID: {data['start']['callSid']}")
                greeting = "Hola!, Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
                audio_response = await text_to_speech(greeting)
                if audio_response:
                    await websocket.send_bytes(audio_response)
                conversation_history.append({"role": "assistant", "content": greeting})

            elif event_type == "media":
                await process_audio_stream(data, websocket, conversation_history)

            elif event_type == "stop":
                logger.info("🚫 Stream detenido")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó la llamada")
    except Exception as e:
        logger.error(f"⚠️ Error en WebSocket: {str(e)}")
    finally:
        call_duration = time.time() - call_start_time
        logger.info(f"📞 Llamada finalizada. Duración total: {call_duration:.2f} seg.")