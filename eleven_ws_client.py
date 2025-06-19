# eleven_ws_client.py
import asyncio
import base64
import contextlib
import json
import logging
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs ― WebSocket TTS client (audio ulaw_8000 directo a Twilio)
# ─────────────────────────────────────────────────────────────────────────────
# · Abre conexión sin headers → la API-key va en el primer mensaje JSON
# · Envía ajustes iniciales (voice_settings, chunk_length_schedule)
# · Cada texto se manda con "flush": True para audio inmediato
# · Recibe audio ya ulaw_8000 → lo reenvía sin conversión
# · Fallback HTTP si el WS falla
# ─────────────────────────────────────────────────────────────────────────────

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket de ElevenLabs que envía audio μ-law directo a Twilio."""

    def __init__(
        self,
        *,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self._send_to_twilio = websocket_send
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{ELEVEN_LABS_VOICE_ID}/stream-input"
            f"?model_id={MODEL_ID}&output_format=ulaw_8000"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._last_text: str = ""
        self._authenticated: bool = False

    # ─────────── Conexión ─────────── #
    async def connect(self) -> None:
        """Abre la conexión WS (si está cerrada) y envía ajustes iniciales."""
        if self.ws and not getattr(self.ws, "closed", False):
            return

        self.ws = await websockets.connect(self.url)
        logger.info("[EL-WS] Conexión WebSocket abierta.")

        # Ajustes iniciales de voz + buffer
        await self._init_connection_settings()

    async def _init_connection_settings(self) -> None:
        """Se envía una sola vez por conexión."""
        init_payload = {
            "xi_api_key": ELEVEN_LABS_API_KEY,
            "text": " ",                       # mantiene viva la conexión
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
                "use_speaker_boost": False,
            },
            "generation_config": {
                # Ajusta si buscas menos latencia / más calidad
                "chunk_length_schedule": [100, 160, 250, 290],
            },
        }
        await self.ws.send(json.dumps(init_payload))

    # ─────────── Enviar texto ─────────── #
    async def send_text(self, text: str) -> None:
        """Envía texto a ElevenLabs (flush=True para audio inmediato)."""
        cleaned = text.strip()
        if not cleaned:
            return

        self._last_text = cleaned
        if not self.ws or getattr(self.ws, "closed", True):
            await self.connect()

        payload = {
            "text": cleaned,
            "flush": True,                     # fuerza generación inmediata
        }
        if not self._authenticated:
            payload["xi_api_key"] = ELEVEN_LABS_API_KEY
            self._authenticated = True

        await self.ws.send(json.dumps(payload))          # type: ignore[arg-type]

        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())

    # ─────────── Recibir audio ─────────── #
    async def _receive_audio_loop(self) -> None:
        try:
            async for message in self.ws:                 # type: ignore[async-for]
                await self._handle_ws_message(message)
        except Exception as exc:
            logger.error(f"[EL-WS] Error de recepción: {exc}")
            await self._fallback_http()

    async def _handle_ws_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
            audio_b64: Optional[str] = payload.get("audio")
            if not audio_b64:
                return

            # ulaw_8000 ya viene listo para Twilio
            mulaw_bytes = base64.b64decode(audio_b64)
            await self._send_audio_to_twilio(mulaw_bytes)
        except Exception as exc:
            logger.warning(f"[EL-WS] Error procesando mensaje WS: {exc}")

    async def _send_audio_to_twilio(self, mulaw_bytes: bytes) -> None:
        msg = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(mulaw_bytes).decode()},
        }
        try:
            await self._send_to_twilio(json.dumps(msg))
        except Exception as exc:
            logger.error(f"[EL-WS] No se pudo enviar audio a Twilio: {exc}")

    # ─────────── Cierre limpio ─────────── #
    async def close(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task

        if self.ws and not getattr(self.ws, "closed", False):
            await self.ws.close()
            logger.info("[EL-WS] Conexión WebSocket cerrada.")

    # ─────────── Fallback HTTP ─────────── #
    async def _fallback_http(self) -> None:
        """Usa la API REST si el WebSocket falla."""
        try:
            logger.error("[EL-WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio  # import diferido

            await send_tts_fallback_to_twilio(
                text=self._last_text,
                stream_sid=self.stream_sid,
                websocket_send=self._send_to_twilio,
            )
        except Exception as exc:
            logger.critical(f"[EL-WS] Fallback HTTP también falló: {exc}")
