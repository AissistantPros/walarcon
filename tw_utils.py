# -*- coding: utf-8 -*-
"""
Módulo de integración Twilio con manejo de audios locales, tiempos de espera
del usuario y tiempos de espera del sistema (IA/ELabs).
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
AUDIO_DIR = Path(__file__).parent / "audio"

# (A) Tiempos de INACTIVIDAD DEL USUARIO
USER_SILENCE_1 = 15   # Reproducir noescucho_1.wav
USER_SILENCE_2 = 25   # Reproducir noescucho_1.wav de nuevo
USER_SILENCE_3 = 30   # Pedir a IA que se despida y colgamos

# (B) Tiempos de RETARDO DEL SISTEMA (IA/ELabs)
SYS_WAIT_1  =  3  # espera_1.wav
SYS_WAIT_2  =  7  # espera_2.wav
SYS_WAIT_3  = 12  # espera_3.wav
SYS_WAIT_4  = 16  # espera_4.wav
SYS_ERR     = 20  # error_sistema.wav
SYS_ENDCALL = 25  # forzar colgado

# ==================================================
# 🔊 FUNCIÓN GENÉRICA PARA ENVIAR AUDIOS (WAV) EN BASE64
# ==================================================
async def play_backup_audio(websocket: WebSocket, stream_sid: str, filename: str):
    """Envía un archivo de audio WAV en Base64 para Twilio."""
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
# 🔀 TAREA PARA CONTROLAR RETARDO DEL SISTEMA
# ==================================================
async def system_wait_tracker(start_time: float, websocket: WebSocket, stream_sid: str, stop_event: asyncio.Event):
    """
    Espera en segundo plano mientras la IA procesa.
    Se reproducen audios de espera a los 3, 7, 12, 16s.
    A los 20s => error_sistema.wav
    A los 25s => forzar cierre de la llamada.

    - Se detiene si la IA termina antes (stop_event.set() desde otro lado).
    - Se maneja con un loop asíncrono que comprueba cada 1s.
    """
    played_stages = set()

    while True:
        await asyncio.sleep(1)
        if stop_event.is_set():
            # La IA terminó, cancelamos la espera
            return

        elapsed = time.time() - start_time

        if elapsed >= SYS_ENDCALL:
            # 25s => forzamos colgado
            logger.error("El sistema tardó 25s. Se forzará la terminación de la llamada.")
            await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
            await asyncio.sleep(1)
            await websocket.close()
            return

        elif elapsed >= SYS_ERR and "SYS_ERR" not in played_stages:
            played_stages.add("SYS_ERR")
            logger.error("El sistema tardó 20s => error_sistema.wav")
            await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
            # Podrías colgar aquí mismo o a los 25s => decido no colgar de inmediato

        elif elapsed >= SYS_WAIT_4 and 4 not in played_stages:
            played_stages.add(4)
            await play_backup_audio(websocket, stream_sid, "espera_4.wav")

        elif elapsed >= SYS_WAIT_3 and 3 not in played_stages:
            played_stages.add(3)
            await play_backup_audio(websocket, stream_sid, "espera_3.wav")

        elif elapsed >= SYS_WAIT_2 and 2 not in played_stages:
            played_stages.add(2)
            await play_backup_audio(websocket, stream_sid, "espera_2.wav")

        elif elapsed >= SYS_WAIT_1 and 1 not in played_stages:
            played_stages.add(1)
            await play_backup_audio(websocket, stream_sid, "espera_1.wav")


# ==================================================
# 🧠 PROCESA LA IA Y DEVUELVE AUDIO
# ==================================================
async def process_full_utterance(
    websocket: WebSocket,
    conversation_history: list, 
    partial_transcript: str,
    stream_sid: str
):
    """
    1. Lanza en paralelo:
       - La llamada a la IA y TTS.
       - Un tracker que reproduce audios de espera cada cierto tiempo.

    2. Si la IA termina antes de 20s:
       - Cancelamos el tracker y enviamos la respuesta.

    3. Si llega a 20s sin terminar:
       - Reproducimos error_sistema.wav
       - A 25s forzamos colgado.

    Lógica:
      - Creamos una Event "stop_event"
      - Iniciamos system_wait_tracker() como task.
      - Hacemos la llamada IA (con timeout).
      - Ponemos stop_event.set() cuando termine la IA normal.

    * Ten en cuenta que si el usuario empieza a hablar
      en medio de la generación de respuesta, no se
      interrumpe la respuesta. (Puedes mejorarlo si quieres).
    """

    logger.info(f"[Conversación] Usuario dice: {partial_transcript}")

    # (1) Preparamos la tarea de "progreso" (audios de espera)
    stop_event = asyncio.Event()
    start_time = time.time()
    tracker_task = asyncio.create_task(system_wait_tracker(start_time, websocket, stream_sid, stop_event))

    try:
        # (2) Llamar a la IA con timeout de 28s (por ejemplo), 
        #     para que no choque con los 25s de colgado.
        ai_response = await asyncio.wait_for(
            asyncio.to_thread(
                generate_openai_response,
                conversation_history + [{"role": "user", "content": partial_transcript}]
            ),
            timeout=28
        )

        # (3) Marcamos que el sistema terminó => paramos audios de espera.
        stop_event.set()

        logger.info(f"IA respondió => {ai_response}")

        # Generamos TTS
        audio_response = await asyncio.to_thread(text_to_speech, ai_response)
        
        # Enviamos audio en chunks ~1 segundo
        chunk_size = 8000
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
        # Si la IA tardó más de 28s => error real
        logger.error("La IA tardó demasiado. error_sistema.wav y colgamos.")
        stop_event.set()  # Cancelamos la tarea de espera
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
        await asyncio.sleep(1)
        await websocket.close()

    finally:
        # Aseguramos que se cancele el tracker en cualquier caso
        if not tracker_task.done():
            tracker_task.cancel()

# ==================================================
# 🎤 PROCESO DE AUDIO ENTRANTE
# ==================================================
async def process_audio_stream(
    data: dict,
    websocket: WebSocket,
    conversation_history: list, 
    stream_sid: str,
    audio_buffer: bytearray
):
    """Procesa el audio entrante desde Twilio (Base64 -> muLaw -> PCM -> STT)."""
    media = data.get("media", {})
    chunk = base64.b64decode(media.get("payload", ""))
    audio_buffer.extend(chunk)
    
    # Procesamos el audio cada ~0.3s
    if len(audio_buffer) >= 2400:
        transcript = speech_to_text(bytes(audio_buffer))
        audio_buffer.clear()

        if transcript:
            # Cuando el usuario termina su frase, lanzamos IA
            await process_full_utterance(
                websocket=websocket,
                conversation_history=conversation_history,
                partial_transcript=transcript,
                stream_sid=stream_sid
            )

# ==================================================
# 💬 DESPEDIDA DE LA IA CUANDO USUARIO NO HABLA
# ==================================================
async def process_farewell_ai(websocket: WebSocket, conversation_history: list, stream_sid: str):
    """
    Pide a la IA generar una despedida breve y reproduce el audio resultante.
    """
    try:
        system_msg = (
            "El usuario ha estado en silencio 30 segundos. "
            "Despidete brevemente (menos de 30 palabras) y cierra la llamada."
        )
        ai_farewell = await asyncio.wait_for(
            asyncio.to_thread(
                generate_openai_response,
                conversation_history + [{"role": "system", "content": system_msg}]
            ),
            timeout=8
        )
        audio_farewell = await asyncio.to_thread(text_to_speech, ai_farewell)

        # Enviamos en chunks
        chunk_size = 8000
        for i in range(0, len(audio_farewell), chunk_size):
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {
                    "payload": base64.b64encode(audio_farewell[i:i+chunk_size]).decode("utf-8")
                }
            }))

        conversation_history.append({"role": "assistant", "content": ai_farewell})

    except asyncio.TimeoutError:
        logger.error("La IA no pudo generar la despedida final a tiempo.")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
    except Exception as e:
        logger.error(f"Error generando la despedida de la IA: {e}")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")

# ==================================================
# 📞 MANEJO PRINCIPAL DE WEBSOCKETS (INACTIVIDAD DEL USUARIO)
# ==================================================
async def handle_twilio_websocket(websocket: WebSocket):
    """
    1. Acepta la conexión y reproduce saludo.wav
    2. Escucha el audio del usuario. Si no habla:
       - A los 15s => noescucho_1.wav
       - A los 25s => noescucho_1.wav
       - A los 30s => la IA despide => colgamos
    3. Si el sistema tarda mucho en responder, se maneja en process_full_utterance con system_wait_tracker.
    """
    await websocket.accept()
    conversation_history = []
    audio_buffer = bytearray()
    stream_sid = None

    # Momento de la última vez que recibimos un audio
    last_user_activity = time.time()
    user_warn_stage = 0

    try:
        # Saludo inicial
        await play_backup_audio(websocket, stream_sid, "saludo.wav")

        while True:
            # Esperamos hasta 5s más allá del umbral más grande de silencio 
            # (30s + un poco) para recibir algo del usuario
            inactivity_limit = 35
            message = await asyncio.wait_for(websocket.receive_text(), timeout=inactivity_limit)
            data = json.loads(message)
            event_type = data.get("event", "")

            if event_type == "start":
                stream_sid = data.get("streamSid")
                logger.info(f"🔄 Nuevo stream: {stream_sid}")

            elif event_type == "media":
                # Usuario habla => reseteamos su contador de inactividad
                last_user_activity = time.time()
                await process_audio_stream(data, websocket, conversation_history, stream_sid, audio_buffer)

            # Revisa inactividad
            elapsed_silence = time.time() - last_user_activity

            # 15s => noescucho_1.wav (una sola vez)
            if elapsed_silence >= USER_SILENCE_1 and user_warn_stage == 0:
                user_warn_stage = 1
                await play_backup_audio(websocket, stream_sid, "noescucho_1.wav")

            # 25s => noescucho_1.wav (segunda vez)
            elif elapsed_silence >= USER_SILENCE_2 and user_warn_stage == 1:
                user_warn_stage = 2
                await play_backup_audio(websocket, stream_sid, "noescucho_1.wav")

            # 30s => IA se despide
            elif elapsed_silence >= USER_SILENCE_3:
                logger.info("⏰ 30s de silencio => la IA se despide.")
                await process_farewell_ai(websocket, conversation_history, stream_sid)
                await asyncio.sleep(2)
                await websocket.close()
                break

    except WebSocketDisconnect:
        logger.info("❌ Usuario colgó.")
    except asyncio.TimeoutError:
        # Nadie habló en ~35s => 
        logger.warning("⏰ Exceso de tiempo sin recibir datos => forzamos despedida IA.")
        await process_farewell_ai(websocket, conversation_history, stream_sid)
        await asyncio.sleep(2)
    except Exception as e:
        logger.error(f"💥 Error crítico: {str(e)}")
        await play_backup_audio(websocket, stream_sid, "error_sistema.wav")
    finally:
        await websocket.close()
