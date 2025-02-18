# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets
"""

import json
import time
import logging
import base64
import asyncio
from fastapi import WebSocket, WebSocketDisconnect
from aiagent import generate_openai_response
from audio_utils import speech_to_text, text_to_speech

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list):
    try:
        # Decodificar audio desde base64
        audio_payload = data.get("media", {}).get("payload", "")
        audio_bytes = base64.b64decode(audio_payload)
        
        if not audio_bytes:
            logger.warning("⚠️ Audio vacío")
            return

        # Transcripción con Whisper
        transcript = await speech_to_text(audio_bytes)
        if not transcript:
            return

        logger.info(f"👤 Usuario: {transcript}")
        conversation_history.append({"role": "user", "content": transcript})

        # Generar respuesta
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
        
        # Convertir a audio (formato PCMU)
        audio_response = await text_to_speech(ai_response)
        if audio_response:
            await websocket.send_bytes(audio_response)
            conversation_history.append({"role": "assistant", "content": ai_response})

    except Exception as e:
        logger.error(f"❌ Error: {str(e)}")

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    call_start_time = time.time()
    
    try:
        # ✅ Handshake crítico con Twilio
        await websocket.send_json({
            "event": "connected",
            "protocol": "Call",
            "version": "1.0"
        })
        
        # Saludo inicial
        greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        audio_greeting = await text_to_speech(greeting)
        if audio_greeting:
            await websocket.send_bytes(audio_greeting)
        conversation_history.append({"role": "assistant", "content": greeting})

        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event", "")
            
            if event_type == "media":
                await process_audio_stream(data, websocket, conversation_history)
            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    except Exception as e:
        logger.error(f"💥 Error crítico: {str(e)}")
    finally:
        call_duration = time.time() - call_start_time
        logger.info(f"⏱ Duración total: {call_duration:.2f}s")