# eleven_ws_client.py
import asyncio
import base64
import contextlib
import json
import logging
import audioop           # type: ignore # amplificación + µ-law
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

# ────────────────────────────────────────────────────────────
#  ElevenLabs ⇄ WebSocket  →  Twilio (μ-law 8 kHz)
# ────────────────────────────────────────────────────────────
# • Pide PCM-16 bit 8 kHz  →   amplifica 2× →  convierte a µ-law
# • Voice-settings + chunk_length_schedule se envían al abrir
# • flush=True en cada texto  →  audio inmediato
# • Keep-alive “ ” cada 10 s  →  evita cierre por inactividad (20 s)
# • Fallback HTTP si el WS falla
# ────────────────────────────────────────────────────────────

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Cliente WebSocket de ElevenLabs que envía μ-law a Twilio."""

    def __init__(
        self,
        *,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self._send_to_twilio = websocket_send

        # ← PCM crudo 8 kHz, luego lo convertimos localmente
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{ELEVEN_LABS_VOICE_ID}/stream-input"
            f"?model_id={MODEL_ID}&output_format=pcm_8000"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_text_sent_at: float = asyncio.get_event_loop().time()
        self._last_text_backup: str = ""
        self._authenticated = False

    # ─────────── Conexión ─────────── #
    async def connect(self) -> None:
        """Abre WS y envía ajustes iniciales (solo una vez)."""
        if self.ws and not getattr(self.ws, "closed", False):
            return

        self.ws = await websockets.connect(self.url)
        logger.info("[EL-WS] Conexión WebSocket abierta.")

        await self._init_connection_settings()

        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _init_connection_settings(self) -> None:
        init_payload = {
            "xi_api_key": ELEVEN_LABS_API_KEY,
            "text": " ",                      # mantiene viva la conexión
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
                "use_speaker_boost": False,
            },
            "generation_config": {
                "chunk_length_schedule": [100, 160, 250, 290],
            },
        }
        await self.ws.send(json.dumps(init_payload))

    # ─────────── Enviar texto ─────────── #
    async def send_text(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            return

        self._last_text_backup = clean
        if not self.ws or getattr(self.ws, "closed", True):
            await self.connect()

        payload = {"text": clean, "flush": True}
        if not self._authenticated:
            payload["xi_api_key"] = ELEVEN_LABS_API_KEY
            self._authenticated = True

        await self.ws.send(json.dumps(payload))          # type: ignore[arg-type]
        self._last_text_sent_at = asyncio.get_event_loop().time()

        # listener
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
            data = json.loads(message)
            audio_b64: Optional[str] = data.get("audio")
            if not audio_b64:
                return

            pcm = base64.b64decode(audio_b64)

            # longitud par para PCM-16
            if len(pcm) % 2:
                pcm = pcm[:-1]

            # amplificación 2×
            pcm_amp = audioop.mul(pcm, 2, 2.0)

            # µ-law 8 kHz
            mulaw = audioop.lin2ulaw(pcm_amp, 2)
            await self._send_audio_to_twilio(mulaw)
        except Exception as exc:
            logger.warning(f"[EL-WS] Error procesando chunk: {exc}")

    async def _send_audio_to_twilio(self, mulaw: bytes) -> None:
        msg = {
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(mulaw).decode()},
        }
        try:
            await self._send_to_twilio(json.dumps(msg))
        except Exception as exc:
            logger.error(f"[EL-WS] No se pudo enviar audio a Twilio: {exc}")

    # ─────────── Keep-alive ─────────── #
    async def _keepalive_loop(self) -> None:
        try:
            while self.ws and not getattr(self.ws, "closed", False):
                await asyncio.sleep(10)
                if asyncio.get_event_loop().time() - self._last_text_sent_at >= 10:
                    try:
                        await self.ws.send(json.dumps({"text": " "}))
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

    # ─────────── Cierre limpio ─────────── #
    async def close(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task

        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()

        if self.ws and not getattr(self.ws, "closed", False):
            await self.ws.close()
            logger.info("[EL-WS] Conexión WebSocket cerrada.")

    # ─────────── Fallback HTTP ─────────── #
    async def _fallback_http(self) -> None:
        try:
            logger.error("[EL-WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio

            await send_tts_fallback_to_twilio(
                text=self._last_text_backup,
                stream_sid=self.stream_sid,
                websocket_send=self._send_to_twilio,
            )
        except Exception as exc:
            logger.critical(f"[EL-WS] Fallback HTTP también falló: {exc}")
