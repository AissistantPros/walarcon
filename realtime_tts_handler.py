#realtime_tts_handler.py

import asyncio
import logging
import base64
import json
from typing import Optional

# Importa la función de streaming de bajo nivel definida en tts_utils.py
from tts_utils import stream_tts_realtime_to_twilio

logger = logging.getLogger(__name__)


class RealTimeTTSHandler:
    """Puente alto‑nivel que orquesta el flujo:

        texto → ElevenLabs → (chunks WAV) → Twilio WebSocket

    Mantiene fuera del archivo de utilidades de audio (``tts_utils.py``) todo lo
    que sea específico de Twilio / gestión de llamadas.
    """

    def __init__(
        self,
        *,
        websocket,  # websockets.WebSocketCommonProtocol | fastapi.WebSocket | cualquier obj. con send_text()
        stream_sid: str,
        api_key: str,
        voice_id: str,
    ) -> None:
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.api_key = api_key
        self.voice_id = voice_id

        self._stream_task: Optional[asyncio.Task] = None
        self.is_streaming: bool = False

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def start_streaming_tts(self, text: str) -> None:
        """Convierte *text* en audio y lo envía a Twilio en tiempo real."""
        if self.is_streaming:
            logger.warning("[RT‑TTS] Ya hay un stream en curso; ignorado.")
            return

        self.is_streaming = True

        async def websocket_send(message: str) -> None:
            """Wrapper fino para abstraer la llamada exacta al WebSocket."""
            try:
                await self.websocket.send_text(message)
            except Exception as exc:
                logger.error(f"[RT‑TTS] Error enviando por WebSocket: {exc}")

        async def _run() -> None:
            try:
                await stream_tts_realtime_to_twilio(
                    text=text,
                    voice_id=self.voice_id,
                    api_key=self.api_key,
                    # El streaming de bajo nivel necesita estas tres call‑backs
                    # (send_audio_callback ya no se usa, pero mantenemos firma).
                    send_audio_callback=lambda _bytes: None,
                    websocket_send=websocket_send,
                    stream_sid=self.stream_sid,
                )
            except asyncio.CancelledError:
                logger.info("[RT‑TTS] Stream cancelado")
                raise
            except Exception as exc:
                logger.error(f"[RT‑TTS] Streaming ElevenLabs → Twilio falló: {exc}")
            finally:
                self.is_streaming = False

        # lanzamos en background para que la llamada que solicita TTS no bloquee
        self._stream_task = asyncio.create_task(_run())

    async def stop_streaming(self) -> None:
        """Cancela (si existe) el stream de audio en curso."""
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        self.is_streaming = False

    # ------------------------------------------------------------------
    # Helpers internos (opcionalmente usados por tts_utils)
    # ------------------------------------------------------------------

    async def _send_audio_to_twilio_realtime(self, audio_data: bytes) -> None:
        """Codifica *audio_data* a base‑64 y lo empuja a Twilio."""
        try:
            payload_b64 = base64.b64encode(audio_data).decode("utf-8")
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload_b64},
            }
            await self.websocket.send_text(json.dumps(message))
            logger.debug(f"[RT‑TTS] Chunk enviado a Twilio: {len(audio_data)} bytes")
        except Exception as exc:
            logger.error(f"[RT‑TTS] No se pudo enviar chunk a Twilio: {exc}")
