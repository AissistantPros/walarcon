# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Manejo de WebSockets
"""

import json
import logging
import base64
import asyncio
import re
from fastapi import WebSocket, WebSocketDisconnect
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Palabras válidas en múltiples idiomas (detectar respuestas cortas útiles)
VALID_SINGLE_WORDS = {
    "es": {"sí", "no", "ok", "vale", "gracias", "claro", "correcto", "perfecto",
           "mañana", "ayer", "hoy", "tarde", "noche", "temprano",
           "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
           "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"},
    
    "en": {"yes", "no", "ok", "thanks", "thank you", "of course", "sure", "fine",
           "tomorrow", "yesterday", "today", "morning", "afternoon", "night",
           "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"},

    "fr": {"oui", "non", "d'accord", "merci", "bien sûr", "parfait", "correct",
           "demain", "hier", "aujourd'hui", "matin", "après-midi", "nuit",
           "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix",
           "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"}
}

async def process_audio_stream(audio_buffer: bytearray, websocket: WebSocket, conversation_history: list, stream_sid: str):
    try:
        if len(audio_buffer) < 1600 * 10:  # Procesar si el buffer tiene al menos 1 segundo de audio
            return

        # Convertir a bytes
        audio_bytes = bytes(audio_buffer)
        audio_buffer.clear()  # Limpiar buffer después de procesar

        # Transcribir con Whisper
        transcript, language = await speech_to_text(audio_bytes)
        if not transcript:
            return

        words = transcript.strip().lower().split()

        # Si es una sola palabra, verificar si es válida en su idioma detectado
        if len(words) == 1 and language in VALID_SINGLE_WORDS and words[0] not in VALID_SINGLE_WORDS[language]:
            logger.warning(f"⚠️ Posible ruido descartado: {transcript} (Idioma: {language})")
            return

        # Si tiene más de una palabra pero sin caracteres alfabéticos, es ruido
        if len(words) > 1 and not re.search(r"[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]", transcript):
            logger.warning(f"⚠️ Transcripción sin sentido descartada: {transcript}")
            return

        logger.info(f"👤 Usuario ({language}): {transcript}")
        conversation_history.append({"role": "user", "content": transcript})

        # Generar respuesta con OpenAI en el mismo idioma
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history, language)

        # Convertir respuesta a audio en el mismo idioma
        audio_response = await asyncio.to_thread(text_to_speech, ai_response, language)
        if audio_response:
            media_message = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_response).decode("utf-8")
                }
            }
            await websocket.send_text(json.dumps(media_message))

            # Enviar marca de fin de audio para Twilio
            mark_message = {
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": "end_of_audio"}
            }
            await websocket.send_text(json.dumps(mark_message))

            conversation_history.append({"role": "assistant", "content": ai_response})

    except Exception as e:
        logger.error(f"❌ Error en process_audio_stream: {str(e)}")

async def handle_twilio_websocket(websocket: WebSocket):
    await websocket.accept()
    conversation_history = []
    stream_sid = None
    audio_buffer = bytearray()  # Buffer para acumular audio

    try:
        # Handshake con Twilio
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
                stream_sid = data.get("streamSid")
                logger.info(f"🎤 Inicio de stream - SID: {stream_sid}")

                # Saludo inicial
                greeting = "Hola! Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
                audio_greeting = await asyncio.to_thread(text_to_speech, greeting, "es")
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
                audio_payload = data.get("media", {}).get("payload", "")
                chunk = base64.b64decode(audio_payload)
                audio_buffer.extend(chunk)  # Acumular audio

                # Procesar audio solo si se ha acumulado suficiente
                await process_audio_stream(audio_buffer, websocket, conversation_history, stream_sid)

            elif event_type == "stop":
                logger.info("🚫 Llamada finalizada")
                break

    except WebSocketDisconnect:
        logger.info("🔌 Usuario colgó")
    except Exception as e:
        logger.error(f"💥 Error crítico en WebSocket: {str(e)}")
    finally:
        await websocket.close()
