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
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list, stream_sid: str):
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
        audio_response = await asyncio.to_thread(text_to_speech, ai_response)
        if audio_response:
            # ✅ Enviar audio como mensaje JSON válido para Twilio
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
        logger.error(f"❌ Error: {str(e)}")

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    stream_sid = None  # Almacenar el SID del stream
    
    try:
        # ✅ Handshake crítico con Twilio
        await websocket.send_json({
            "event": "connected",
            "protocol": "Call",
            "version": "1.0"
        })
        
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event_type = data.get("event", "")
            
            if event_type == "start":
                # Capturar stream_sid para enviar audio
                stream_sid = data.get("streamSid")
                logger.info(f"🎤 Inicio de stream - SID: {stream_sid}")
                
                # Saludo inicial
                greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
                audio_greeting = await asyncio.to_thread(text_to_speech, greeting)
                if audio_greeting:
                    media_message = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": base64.b64encode(audio_greeting).decode("utf-8")
                        }
                    }
                    await websocket.send_text(json.dumps(media_message))
                conversation_history.append({"role": "assistant", "content": greeting})
                
            elif event_type == "media" and stream_sid:
                await process_audio_stream(data, websocket, conversation_history, stream_sid)
                
            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    except Exception as e:
        logger.error(f"💥 Error crítico: {str(e)}")
    finally:
        await websocket.close()