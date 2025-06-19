#eleven_ws_client.py

import asyncio
import base64
import contextlib
import json
import audioop # type: ignore
import logging
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ---------------------------------------------------------------------------
# ElevenLabs — WebSocket TTS client (única fuente de verdad)
# ---------------------------------------------------------------------------
# 1) Conecta al WebSocket (connect)
# 2) Envía texto (send_text)
# 3) Recibe audio (loop interno)
# 4) Convierte el audio a μ‑law 8 kHz (convert_to_mulaw)
# 5) Empuja chunks a Twilio (send_audio_to_twilio)
# 6) Cierra la conexión (close)
#    Si algo falla se invoca un fallback HTTP (eleven_http_client)
# ---------------------------------------------------------------------------

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket a ElevenLabs con envío directo a Twilio."""

    def __init__(
        self,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self.websocket_send = websocket_send  # -> función send_text de Starlette/FastAPI
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream-input?model_id={MODEL_ID}"
        )
       

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._last_text: str = ""  # Para fallback en caso de corte
        self._authenticated: bool = False

    # ------------------------------------------------------------------
    # 1) Conexión
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Abre la conexión WebSocket si no existe o está cerrada."""
        if self.ws and self.ws.open:
            return
        self.ws = await websockets.connect(self.url)  # sin extra_headers

        logger.info("[EL‑WS] Conexión WebSocket abierta.")

    # ------------------------------------------------------------------
    # 2) Enviar texto
    # ------------------------------------------------------------------
    async def send_text(self, text: str) -> None:
        """Envía *text* a ElevenLabs (debe haber conexión previa)."""
        cleaned = text.strip()
        if not cleaned:
            logger.debug("[EL‑WS] Texto vacío, no se envía a ElevenLabs.")
            return

        self._last_text = cleaned  # Lo guardamos por si hay que hacer fallback

        # Asegura la conexión
        if not self.ws or not self.ws.open:
            await self.connect()

        # Enviar texto en JSON
        # Si es la primera vez, autenticamos en el mismo mensaje
        if not self._authenticated:
            payload = {
                "xi_api_key": ELEVEN_LABS_API_KEY,
                "text": cleaned
            }
            self._authenticated = True
        else:
            payload = {"text": cleaned}

        await self.ws.send(json.dumps(payload))
        logger.debug("[EL-WS] Texto enviado a ElevenLabs.")

        # Arranca la recepción si no corre aún
        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())

    # ------------------------------------------------------------------
    # 3) Recepción de audio (loop)
    # ------------------------------------------------------------------
    async def _receive_audio_loop(self) -> None:
        try:
            async for message in self.ws:  # type: ignore[arg-type]
                await self._handle_ws_message(message)
        except websockets.ConnectionClosedOK:
            logger.info("[EL‑WS] Conexión cerrada limpiamente por el servidor.")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EL‑WS] Error en recepción: {e}")
            await self._fallback_http()

    # ------------------------------------------------------------------
    # 4) Convertir audio y 5) Enviarlo a Twilio
    # ------------------------------------------------------------------
    async def _handle_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
            audio_b64: Optional[str] = payload.get("audio")
            if not audio_b64:
                logger.debug("[EL‑WS] Mensaje WS sin audio — ignorado.")
                return

            pcm_bytes = base64.b64decode(audio_b64)
            mulaw_bytes = self._convert_to_mulaw(pcm_bytes)
            await self._send_audio_to_twilio(mulaw_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[EL‑WS] Error procesando mensaje WS: {e}")

    @staticmethod
    def _convert_to_mulaw(pcm_bytes: bytes) -> bytes:
        """Convierte PCM 16‑bit 8 kHz a μ‑law (8 bit 8 kHz)."""
        try:
            return audioop.lin2ulaw(pcm_bytes, 2)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EL‑WS] Conversión a μ‑law falló: {e}")
            return b""

    async def _send_audio_to_twilio(self, mulaw_bytes: bytes) -> None:
        if not mulaw_bytes:
            return
        message = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(mulaw_bytes).decode("utf-8")},
        }
        try:
            await self.websocket_send(json.dumps(message))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[EL‑WS] No se pudo enviar audio a Twilio: {e}")

    # ------------------------------------------------------------------
    # 6) Cerrar conexión limpìa
    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Cierra la conexión y cancela la tarea de recepción si existe."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task

        if self.ws and self.ws.open:
            await self.ws.close()
            logger.info("[EL‑WS] Conexión WebSocket cerrada.")

    # ------------------------------------------------------------------
    # Fallback HTTP
    # ------------------------------------------------------------------
    async def _fallback_http(self) -> None:
        """Llama al cliente HTTP de respaldo si la conexión WS falla."""
        try:
            logger.error("[EL‑WS] Activando fallback HTTP.")
            # Importación diferida para evitar ciclos
            from eleven_http_client import send_tts_fallback_to_twilio  # noqa: WPS433

            await send_tts_fallback_to_twilio(
                text=self._last_text,
                stream_sid=self.stream_sid,
                websocket_send=self.websocket_send,
            )
        except Exception as e:  # noqa: BLE001
            logger.critical(f"[EL‑WS] Fallback HTTP también falló: {e}")
