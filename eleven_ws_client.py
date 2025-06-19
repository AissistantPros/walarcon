# eleven_ws_client.py
import asyncio
import base64
import contextlib
import json
import audioop            # type: ignore
import logging
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ---------------------------------------------------------------------------
# ElevenLabs — WebSocket TTS client
# ---------------------------------------------------------------------------
# 1) connect()        – abre la conexión WS (sin headers)
# 2) send_text()      – envía texto (primer mensaje incluye la API-key)
# 3) _receive_audio_loop() – escucha audio, lo convierte a μ-law y lo manda a Twilio
# 4) close()          – cierra y cancela tareas
# 5) _fallback_http() – usa REST si el WS falla
# ---------------------------------------------------------------------------

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str           = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket de ElevenLabs que envía audio directamente a Twilio."""

    # ------------------------------------------------------------------ #
    # ctor
    # ------------------------------------------------------------------ #
    def __init__(
        self,
        *,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self._send_to_twilio = websocket_send          # websocket.send_text de FastAPI
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{ELEVEN_LABS_VOICE_ID}/stream-input?model_id={MODEL_ID}"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._last_text: str = ""       # para fallback
        self._authenticated = False

    # ------------------------------------------------------------------ #
    # 1) Conexión
    # ------------------------------------------------------------------ #
    async def connect(self) -> None:
        """Abre la conexión WebSocket si aún no existe o está cerrada."""
        if self.ws and not self.ws.closed:
            return

        # NO enviamos headers – la API-key va en el primer mensaje JSON
        self.ws = await websockets.connect(self.url)
        logger.info("[EL-WS] Conexión WebSocket abierta.")

    # ------------------------------------------------------------------ #
    # 2) Enviar texto
    # ------------------------------------------------------------------ #
    async def send_text(self, text: str) -> None:
        """Envía *text* a ElevenLabs (crea conexión si es necesario)."""
        cleaned = text.strip()
        if not cleaned:
            logger.debug("[EL-WS] Texto vacío: no se envía.")
            return

        self._last_text = cleaned

        if not self.ws or self.ws.closed:
            await self.connect()

        # Primer mensaje: incluye la API-key
        payload = (
            {"xi_api_key": ELEVEN_LABS_API_KEY, "text": cleaned}
            if not self._authenticated
            else {"text": cleaned}
        )
        self._authenticated = True

        await self.ws.send(json.dumps(payload))
        logger.debug("[EL-WS] Texto enviado a ElevenLabs.")

        # Arranca el listener de audio si no está corriendo
        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())

    # ------------------------------------------------------------------ #
    # 3) Loop de recepción de audio
    # ------------------------------------------------------------------ #
    async def _receive_audio_loop(self) -> None:
        try:
            async for message in self.ws:                       # type: ignore[async-for]
                await self._handle_ws_message(message)
        except websockets.ConnectionClosedOK:
            logger.info("[EL-WS] Conexión WS cerrada por servidor.")
        except Exception as exc:
            logger.error(f"[EL-WS] Error en recepción: {exc}")
            await self._fallback_http()

    async def _handle_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
            audio_b64: Optional[str] = payload.get("audio")
            if not audio_b64:
                return

            pcm_bytes = base64.b64decode(audio_b64)
            mulaw_bytes = self._convert_to_mulaw(pcm_bytes)
            if mulaw_bytes:
                await self._send_audio_to_twilio(mulaw_bytes)
        except Exception as exc:
            logger.warning(f"[EL-WS] Error procesando mensaje WS: {exc}")

    # ------------------------------------------------------------------ #
    # 4) Conversión a μ-law y envío a Twilio
    # ------------------------------------------------------------------ #
    @staticmethod
    def _convert_to_mulaw(pcm_bytes: bytes) -> bytes:
        """Convierte PCM 16-bit / 8 kHz ➜ μ-law 8 kHz."""
        try:
            return audioop.lin2ulaw(pcm_bytes, 2)
        except Exception as exc:
            logger.error(f"[EL-WS] Conversión a μ-law falló: {exc}")
            return b""

    async def _send_audio_to_twilio(self, mulaw_bytes: bytes) -> None:
        message = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(mulaw_bytes).decode()},
        }
        try:
            await self._send_to_twilio(json.dumps(message))
        except Exception as exc:
            logger.error(f"[EL-WS] No se pudo enviar audio a Twilio: {exc}")

    # ------------------------------------------------------------------ #
    # 5) Cierre limpio
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """Cancela tareas y cierra el WebSocket si sigue abierto."""
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task

        if self.ws and not self.ws.closed:
            await self.ws.close()
            logger.info("[EL-WS] Conexión WebSocket cerrada.")

    # ------------------------------------------------------------------ #
    # 6) Fallback HTTP (REST)
    # ------------------------------------------------------------------ #
    async def _fallback_http(self) -> None:
        """Usa la API REST de ElevenLabs si el WebSocket falla."""
        try:
            logger.error("[EL-WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio  # lazy-import

            await send_tts_fallback_to_twilio(
                text=self._last_text,
                stream_sid=self.stream_sid,
                websocket_send=self._send_to_twilio,
            )
        except Exception as exc:
            logger.critical(f"[EL-WS] Fallback HTTP también falló: {exc}")
