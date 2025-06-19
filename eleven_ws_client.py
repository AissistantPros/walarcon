import asyncio
import base64
import contextlib
import json
import logging
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ---------------------------------------------------------------------------
# ElevenLabs — WebSocket TTS client
#   * Conecta al WS (connect)
#   * Envía texto (send_text)
#   * Recibe audio μ‑law 8 kHz ya listo (loop)
#   * Reenvía chunks a Twilio (send_audio_to_twilio)
#   * Cierra la conexión (close)
#   * Fallback HTTP si el WS falla
# ---------------------------------------------------------------------------

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket a ElevenLabs con reenvío directo a Twilio."""

    def __init__(
        self,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self.websocket_send = websocket_send  # send_text del WS de Twilio
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream-input"
            f"?model_id={MODEL_ID}&output_format=ulaw_8000"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._last_text: str = ""
        self._authenticated: bool = False

    # ------------------------------------------------------------------
    # 1) Conexión
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Abre la conexión WebSocket si no existe o está cerrada."""
        if self.ws and not self.ws.closed:
            return
        self.ws = await websockets.connect(self.url)
        logger.info("[EL‑WS] Conexión WebSocket abierta.")

    # ------------------------------------------------------------------
    # 2) Enviar texto
    # ------------------------------------------------------------------
    async def send_text(self, text: str) -> None:
        """Envía *text* a ElevenLabs."""
        cleaned = text.strip()
        if not cleaned:
            logger.debug("[EL‑WS] Texto vacío, ignorado.")
            return

        self._last_text = cleaned  # Para fallback

        if not self.ws or self.ws.closed:
            await self.connect()

        # Primer mensaje lleva la API‑key
        if not self._authenticated:
            payload = {"xi_api_key": ELEVEN_LABS_API_KEY, "text": cleaned}
            self._authenticated = True
        else:
            payload = {"text": cleaned}

        await self.ws.send(json.dumps(payload))
        logger.debug("[EL‑WS] Texto enviado a ElevenLabs.")

        # Lanza recepción si no existe
        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())

    # ------------------------------------------------------------------
    # 3) Recepción de audio μ‑law (loop)
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
    # 4) Procesar cada mensaje y reenviar a Twilio
    # ------------------------------------------------------------------
    async def _handle_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
            audio_b64: Optional[str] = payload.get("audio")
            if not audio_b64:
                logger.debug("[EL‑WS] Mensaje WS sin audio — ignorado.")
                return

            mulaw_bytes = base64.b64decode(audio_b64)  # Ya viene en μ‑law 8 kHz
            await self._send_audio_to_twilio(mulaw_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[EL‑WS] Error procesando mensaje WS: {e}")

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
    # 5) Cerrar conexión limpia
    # ------------------------------------------------------------------
    async def close(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task

        if self.ws and not self.ws.closed:
            await self.ws.close()
            logger.info("[EL‑WS] Conexión WebSocket cerrada.")

    # ------------------------------------------------------------------
    # 6) Fallback HTTP
    # ------------------------------------------------------------------
    async def _fallback_http(self) -> None:
        try:
            logger.error("[EL‑WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio  # noqa: WPS433

            await send_tts_fallback_to_twilio(
                text=self._last_text,
                stream_sid=self.stream_sid,
                websocket_send=self.websocket_send,
            )
        except Exception as e:  # noqa: BLE001
            logger.critical(f"[EL‑WS] Fallback HTTP también falló: {e}")
