# tw_utils.py

import json
import base64
import time
import asyncio
import logging
from audio_in import AudioBuffer
from fastapi import WebSocket

AUDIO_DIR = "audio"

# Tiempos de silencio
SILENCE_WARNING_1 = 15  # 15s
SILENCE_WARNING_2 = 25  # 25s
SILENCE_HANGUP = 30      # 30s

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio, recibe audio,
    detecta silencio prolongado y reproduce audios pregrabados 
    si es necesario.
    """

    def __init__(self):
        # Creamos la instancia de AudioBuffer con threshold simple
        self.audio_buffer = AudioBuffer(silence_threshold=50)

        # Control de inactividad total (para disparar "noescucho" y colgar)
        self.last_user_activity = time.time()

        # Para controlar que solo reproduzcamos "noescucho" 2 veces
        self.warned_once = False
        self.warned_twice = False

        # Para marcar si ya colgamos la llamada
        self.call_ended = False

        # Guardar el stream SID para enviar audio a Twilio
        self.stream_sid = None

    async def handle_twilio_websocket(self, websocket: WebSocket):
        """
        Entrada principal de la conexión con Twilio.
        Recibe los eventos 'start', 'media', 'stop', etc.
        """
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Iniciamos la tarea que verificará el silencio prolongado
        silence_task = asyncio.create_task(self._check_silence(websocket))

        try:
            while True:
                # Esperamos un mensaje de Twilio
                message = await websocket.receive_text()
                data = json.loads(message)

                event_type = data.get("event")
                if event_type == "start":
                    # Twilio inicia un nuevo stream
                    self.stream_sid = data.get("streamSid")
                    logger.info(f"Nuevo stream SID: {self.stream_sid}")

                elif event_type == "media":
                    # Llega un chunk de audio base64
                    payload_base64 = data["media"]["payload"]
                    chunk = base64.b64decode(payload_base64)
                    await self._process_audio_chunk(chunk)

                elif event_type == "stop":
                    logger.info("Twilio envió evento 'stop'. Cerrando websocket.")
                    await self._hangup_call(websocket)
                    break

        except asyncio.exceptions.CancelledError:
            # Ocurre cuando cancelamos la tarea
            pass
        except Exception as e:
            logger.error(f"Error en handle_twilio_websocket: {e}")
            await self._hangup_call(websocket)
        finally:
            # Cancelamos la tarea de silencio si sigue viva
            silence_task.cancel()
            await websocket.close()
            logger.info("WebSocket con Twilio cerrado.")

    async def _process_audio_chunk(self, chunk: bytes):
        """
        Recibe un chunk de audio, lo pasa al AudioBuffer.
        Si se completa un bloque (2s de silencio o 10s totales),
        lo logueamos por ahora (luego lo mandaremos a STT).
        """
        block = self.audio_buffer.process_chunk(chunk)

        # Si hay "voz" en este chunk, reiniciamos el contador de inactividad
        if block is not None or self.audio_buffer._has_voice(chunk):
            self.last_user_activity = time.time()

        # Si se formó un bloque completo
        if block:
            logger.info(f"Bloque de audio completo (size {len(block)} bytes).")
            # Más adelante, aquí llamaremos a STT.

    async def _check_silence(self, websocket: WebSocket):
        """
        Tarea en segundo plano que verifica si el usuario 
        lleva 15/25/30s sin hablar.
        """
        while not self.call_ended:
            await asyncio.sleep(1)
            elapsed = time.time() - self.last_user_activity

            if elapsed >= SILENCE_HANGUP:
                # 30s sin hablar => colgamos
                logger.info("30s de silencio. Colgando la llamada.")
                await self._hangup_call(websocket)
                break

            elif elapsed >= SILENCE_WARNING_2 and not self.warned_twice:
                self.warned_twice = True
                logger.info("25s de silencio. Reproduciendo noescucho_1.wav (2a vez).")
                await self._play_audio_file(websocket, "noescucho_1.wav")

            elif elapsed >= SILENCE_WARNING_1 and not self.warned_once:
                self.warned_once = True
                logger.info("15s de silencio. Reproduciendo noescucho_1.wav (1a vez).")
                await self._play_audio_file(websocket, "noescucho_1.wav")

    async def _hangup_call(self, websocket: WebSocket):
        """
        Marca la llamada como finalizada y cierra la conexión WS.
        """
        if not self.call_ended:
            self.call_ended = True
            logger.info("Terminando la llamada.")
            await websocket.close()

    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        """
        Envía un WAV a Twilio en base64.
        Debe ser mono 8kHz, 16 bits, para que Twilio lo reproduzca bien.
        """
        if not self.stream_sid:
            logger.warning("No hay stream SID, no puedo reproducir audio.")
            return

        try:
            filepath = f"{AUDIO_DIR}/{filename}"
            with open(filepath, "rb") as f:
                wav_data = f.read()

            encoded = base64.b64encode(wav_data).decode("utf-8")

            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded}
            }))
            logger.info(f"Reproduciendo: {filename}")
        except FileNotFoundError:
            logger.error(f"No se encontró el archivo: {filepath}")
        except Exception as e:
            logger.error(f"Error reproduciendo {filename}: {e}")
