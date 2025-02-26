# -*- coding: utf-8 -*-
"""
Módulo de integración Twilio con manejo de audios locales y tiempos de espera.
"""
import json
import logging
import base64
import asyncio
import time
import os
from pathlib import Path
from fastapi import WebSocket, WebSocketDisconnect
from audio_utils import speech_to_text, text_to_speech
from aiagent import generate_openai_response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================
# 🔧 CONFIGURACIÓN PRINCIPAL
# ==================================================
MAX_CHUNK_SECS = 5
AUDIO_DIR = Path(__file__).parent / "audio"  # Ruta a la carpeta de audios

# Mapeo de tiempos de espera a archivos de audio
TIMEOUT_STAGES = {
    5: "espera_4.wav",     # 5 segundos: "Un segundo, por favor"
    9: "espera_1.wav",     # 9 segundos: "Permítame un momento..."
    13: "espera_3.wav",    # 13 segundos: "Estoy verificando..."
    17: "noescucho_1.wav", # 17 segundos: "No le escucho..."
    22: "error_sistema.wav"# 22 segundos: Error crítico
}

# ==================================================
# 🔊 FUNCIÓN PARA REPRODUCIR AUDIOS LOCALES
# ==================================================
async def play_backup_audio(websocket: WebSocket, stream_sid: str, filename: str):
    """Reproduce un archivo de audio local"""
    try:
        with open(AUDIO_DIR / filename, "rb") as f:
            audio_data = f.read()
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_data).decode("utf-8")
                }
            }))
            logger.info(f"Reproduciendo: {filename}")
    except Exception as e:
        logger.error(f"Error al cargar {filename}: {str(e)}")

# ==================================================
# 🎤 MANEJO DEL FLUJO DE AUDIO
# ==================================================
async def process_audio_stream(data: dict, websocket: WebSocket, conversation_history: list, 
                              stream_sid: str, audio_buffer: bytearray):
    """Procesa el audio entrante desde Twilio"""
    media = data.get("media", {})
    chunk = base64.b64decode(media.get("payload", ""))
    audio_buffer.extend(chunk)
    
    if len(audio_buffer) >= 2400:  # 0.3 segundos de audio
        transcript = speech_to_text(bytes(audio_buffer))
        if transcript:
            await process_full_utterance(
                websocket=websocket,
                conversation_history=conversation_history,
                partial_transcript=transcript,
                stream_sid=stream_sid
            )
        audio_buffer.clear()

# ==================================================
# 🧠 PROCESAMIENTO DE RESPUESTAS DE IA
# ==================================================
async def process_full_utterance(websocket: WebSocket, conversation_history: list, 
                                partial_transcript: str, stream_sid: str):
    """Genera y envía la respuesta de la IA"""
    logger.info(f"[Conversación] Usuario dice: {partial_transcript}")
    
    try:
        # Reproducir audio de espera inicial
        await play_backup_audio(websocket, stream_sid, "espera_1.wav")
        
        # Obtener respuesta de IA
        start_ai = time.time()
        ai_response = await asyncio.wait_for(
            asyncio.to_thread(
                generate_openai_response,
                conversation_history + [{"role": "user", "content": partial_transcript}]
            ),
            timeout=8  # Tiempo máximo para la IA
        )
        
        # Generar audio de respuesta
        audio_response = await asyncio.to_thread(text_to_speech, ai_response)
        
        # Enviar audio en chunks
        chunk_size = 8000  # 1 segundo por chunk
        for i in range(0, len(audio_response), chunk_size):
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_response[i:i+chunk_size]).decode("utf-8")
                }
            }))
        
        # Actualizar historial
        conversation_history.extend([
            {"role": "user", "content": partial_transcript},
            {"role": "assistant", "content": ai_response}
        ])
        
    except asyncio.TimeoutError:
        logger.error("¡Tiempo de espera agotado!")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
        await websocket.close()

# ==================================================
# 📞 MANEJO PRINCIPAL DE WEBSOCKETS
# ==================================================
async def handle_twilio_websocket(websocket: WebSocket):
    """Maneja la conexión WebSocket con Twilio"""
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None
    last_activity = time.time()
    timeout_stage = 0

    try:
        # Enviar saludo inicial
        await play_backup_audio(websocket, stream_sid, "saludo.wav")
        
        while True:
            message = await asyncio.wait_for(websocket.receive_text(), timeout=25)
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"🔄 Nuevo stream: {stream_sid}")

            elif event_type == "media":
                last_activity = time.time()
                await process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer)
                timeout_stage = 0  # Resetear contador

            # Manejo de tiempos de espera
            elapsed_time = time.time() - last_activity
            if elapsed_time >= 5 and timeout_stage < 1:
                await play_backup_audio(websocket, stream_sid, TIMEOUT_STAGES[5])
                timeout_stage = 1
            elif elapsed_time >= 9 and timeout_stage < 2:
                await play_backup_audio(websocket, stream_sid, TIMEOUT_STAGES[9])
                timeout_stage = 2
            elif elapsed_time >= 13 and timeout_stage < 3:
                await play_backup_audio(websocket, stream_sid, TIMEOUT_STAGES[13])
                timeout_stage = 3
            elif elapsed_time >= 17 and timeout_stage < 4:
                await play_backup_audio(websocket, stream_sid, TIMEOUT_STAGES[17])
                timeout_stage = 4
            elif elapsed_time >= 22:
                await play_backup_audio(websocket, stream_sid, TIMEOUT_STAGES[22])
                await asyncio.sleep(2)
                await websocket.close()
                break

    except WebSocketDisconnect:
        logger.info("❌ Usuario colgó")
    except Exception as e:
        logger.error(f"💥 Error crítico: {str(e)}")
    finally:
        await websocket.close()