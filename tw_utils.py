# tw_utils.py

import json
import base64
import time
import asyncio
import logging
from fastapi import WebSocket
from google_stt_streamer import GoogleSTTStreamer

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio. 
    En vez de filtrar silencios manualmente, usamos Google STT en streaming.
    """

    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = GoogleSTTStreamer()  # Nuestro objeto de streaming STT
        self.google_task = None  # Tarea asíncrona que escuchará resultados

    async def handle_twilio_websocket(self, websocket: WebSocket):
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Iniciamos la tarea asíncrona que maneja la respuesta de Google
        self.google_task = asyncio.create_task(self._listen_google_results())

        try:
            while True:
                message = await websocket.receive_text()
                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "start":
                    # Nuevo stream de Twilio
                    self.stream_sid = data["streamSid"]
                    logger.info(f"Nuevo stream SID: {self.stream_sid}")
                    await self._play_audio_file(websocket, "saludo.wav")

                elif event_type == "media":
                    # Llega chunk de audio base64
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)
                    # Enviamos el chunk a Google STT
                    self.stt_streamer.add_audio_chunk(mulaw_chunk)

                elif event_type == "stop":
                    logger.info("Twilio envió evento 'stop'. Colgando.")
                    await self._hangup_call(websocket)
                    break

        except Exception as e:
            logger.error(f"Error en handle_twilio_websocket: {e}")
            await self._hangup_call(websocket)
        finally:
            # Si no se había colgado, colgamos
            if not self.call_ended:
                await self._hangup_call(websocket)
            logger.info("WebSocket con Twilio cerrado.")
            await websocket.close()

    async def _listen_google_results(self):
        """
        Tarea que recibe los resultados de Google STT en tiempo real.
        Por cada 'result':
         - if result.is_final => mandar a GPT => TTS => reproducir
         - else => log parcial
        """
        async for result in self.stt_streamer.recognize_stream():
            transcript = result.alternatives[0].transcript
            if result.is_final:
                logger.info(f"USUARIO (final): {transcript}")
                # Aquí llamas a GPT
                await self._process_final_transcript(transcript)
            else:
                logger.info(f"(parcial) => {transcript}")

    async def _process_final_transcript(self, transcript: str):
        """
        Aquí llamamos a GPT, luego a TTS, luego reproducimos en Twilio.
        Lo dejo como ejemplo. Personalízalo a tu gusto.
        """
        logger.info(f"Procesando la transcripción final: {transcript}")

        # 1. Llama a GPT
        #    response = <TU CÓDIGO GPT> 
        response = "Ejemplo de respuesta de GPT"

        # 2. Convierte a TTS (ElevenLabs o tu preferido)
        #    tts_audio = <TU CÓDIGO TTS> (retorna bytes PCM/WAV)
        tts_audio = b""  # Aquí pones la lógica real

        # 3. Envía a Twilio
        #    Debería ser algo como: await self._play_audio_data(websocket, tts_audio)
        #    pero ojo, necesitarás la referencia a 'websocket'. Ver diseño final.
        #    Por simplicidad, muestro un log:
        logger.info(f"(TTS) Respuesta IA: {response}")

    async def _hangup_call(self, websocket: WebSocket):
        if not self.call_ended:
            self.call_ended = True
            logger.info("Terminando la llamada.")
            # Cerramos Google STT
            await self.stt_streamer.close()
            if self.google_task:
                self.google_task.cancel()
            try:
                await websocket.close()
            except:
                pass

    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        """
        Envía un WAV pregrabado a Twilio. 
        Debe ser mono 8kHz, 16 bits. 
        """
        if not self.stream_sid:
            return
        try:
            filepath = f"{AUDIO_DIR}/{filename}"
            with open(filepath, "rb") as f:
                wav_data = f.read()

            import base64
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
