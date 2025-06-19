import asyncio
import base64
import contextlib
import json
import logging
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ---------------------------------------------------------------------------
# ElevenLabs — WebSocket TTS client (versión cabecera, websockets >= 11)
# ---------------------------------------------------------------------------
# · Autenticación vía header `xi-api-key`
# · output_format=ulaw_8000 (audio listo para Twilio)
# · Reenvío directo a Twilio
# ---------------------------------------------------------------------------

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket (cabecera) para ElevenLabs TTS."""

    def __init__(
        self,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self.websocket_send = websocket_send
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}"
            f"/stream-input?model_id={MODEL_ID}&output_format=ulaw_8000"
        )
        self.headers = {"xi-api-key": ELEVEN_LABS_API_KEY}

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._last_text: str = ""
        # ya no se necesita flag de autenticación porque el header va siempre

    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """Conecta si no está abierta."""
        if self.ws and self.ws.open:
            return
        self.ws = await websockets.connect(self.url, extra_headers=self.headers)
        logger.info("[EL-WS] Conexión WebSocket abierta.")

    # ------------------------------------------------------------------
    async def send_text(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return

        self._last_text = cleaned
        if not self.ws or not self.ws.open:
            await self.connect()

        await self.ws.send(json.dumps({"text": cleaned}))  # type: ignore[arg-type]
        logger.debug("[EL-WS] Texto enviado a ElevenLabs.")

        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())

    # ------------------------------------------------------------------
    async def _receive_audio_loop(self) -> None:
        try:
            async for message in self.ws:  # type: ignore[arg-type]
                await self._handle_ws_message(message)
        except websockets.ConnectionClosedOK:
            logger.info("[EL-WS] Conexión cerrada limpiamente por el servidor.")
        except Exception as e:
            logger.error(f"[EL-WS] Error de recepción: {e}")
            await self._fallback_http()

    # ------------------------------------------------------------------
    async def _handle_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
            audio_b64 = payload.get("audio")
            if not audio_b64:
                return
            mulaw_bytes = base64.b64decode(audio_b64)  # ya μ-law 8 kHz
            await self._send_audio_to_twilio(mulaw_bytes)
        except Exception as e:
            logger.warning(f"[EL-WS] Error procesando mensaje WS: {e}")

    async def _send_audio_to_twilio(self, mulaw_bytes: bytes) -> None:
        if not mulaw_bytes:
            return
        msg = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(mulaw_bytes).decode("utf-8")},
        }
        await self.websocket_send(json.dumps(msg))

    # ------------------------------------------------------------------
    async def close(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self.ws and self.ws.open:
            await self.ws.close()
            self.ws = None
            logger.info("[EL-WS] Conexión WebSocket cerrada.")

    # ------------------------------------------------------------------
    async def _fallback_http(self) -> None:
        try:
            logger.error("[EL-WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio  # noqa: WPS433

            await send_tts_fallback_to_twilio(
                text=self._last_text,
                stream_sid=self.stream_sid,
                websocket_send=self.websocket_send,
            )
        except Exception as e:
            logger.critical(f"[EL-WS] Fallback HTTP también falló: {e}")
