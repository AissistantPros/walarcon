#tts_utils.py
import base64
import json
import time
import logging
import audioop  # type: ignore # <-- Asegúrate de importar audioop
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings
import numpy as np
import io
import wave
import struct
from typing import Callable, Optional
import httpx


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO) # Cambia a DEBUG para ver más detalles si es necesario

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

# Inicializar el cliente una vez. Añadimos un try-except por robustez.
try:
    elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)
except Exception as e:
    logger.error(f"[TTS] FALLO AL INICIALIZAR CLIENTE ELEVENLABS: {e}")
    elevenlabs_client = None # Para evitar errores si falla la inicialización

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto a voz utilizando ElevenLabs, intenta amplificar el audio,
    y devuelve el audio en formato mu-law (raw PCM convertido) como bytes.
    Twilio espera audio en formato mu-law (8 kHz, mono, 8 bits).
    
    Args:
        text (str): Texto a convertir.
        
    Returns:
        bytes: Audio en formato mu-law o b"" en caso de error.
    """
    if not elevenlabs_client:
        logger.error("[TTS] Cliente ElevenLabs no disponible. No se puede generar audio.")
        return b""

    start_total = time.perf_counter()
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=ELEVEN_LABS_VOICE_ID,
            model_id="eleven_multilingual_v2", # O el modelo que estés usando
            voice_settings=VoiceSettings(
                stability=0.75,         # Ajustado ligeramente para equilibrio
                style=0.45,             # 'style' es el actual para la similitud deseada
                use_speaker_boost=False, # PROBAR ESTO para mejorar "presencia" de la voz
                speed=1.2               # Un poco más rápido que normal, ajusta a 1.0 si prefieres
                
            ),
            output_format="pcm_8000"  # Solicita raw PCM a 8000 Hz, 16-bit
        )
        audio_data_pcm_16bit = b"".join(audio_stream)
        
        if not audio_data_pcm_16bit:
            logger.warning("[TTS] Stream de ElevenLabs no devolvió datos de audio.")
            return b""

        # --- INICIO: Amplificar el audio PCM con NumPy y limitar picos ---
        audio_data_to_convert = audio_data_pcm_16bit  # por defecto, audio original
        try:
            volume_factor = 2.5     # ⬅️ Ajusta aquí (prueba 2.5–3.0)

            # 1) bytes → ndarray int16  (PCM 16-bit little-endian)
            pcm_array = np.frombuffer(audio_data_pcm_16bit, dtype=np.int16)

            # 2) Aplica ganancia
            amplified = pcm_array.astype(np.float32) * volume_factor

            # 3) Limita al rango permitido ±32768
            limited = np.clip(amplified, -32768, 32767).astype(np.int16)

            # 4) ndarray → bytes
            audio_data_to_convert = limited.tobytes()

            # logger.info(f"[TTS] Audio PCM amplificado x{volume_factor} con limitador")
        except Exception as amp_err:
            logger.warning(f"[TTS] Error al amplificar con NumPy: {amp_err}. Usando audio original.")
        # --- FIN: Amplificar el audio PCM ---
        
        # Convertir el audio PCM (posiblemente amplificado) a mu-law (8 bits)
        try:
            mulaw_data = audioop.lin2ulaw(audio_data_to_convert, 2)
        except audioop.error as conv_audio_err:
            logger.error(f"[TTS] Error de audioop al convertir a mu-law: {conv_audio_err}")
            return b""
        except Exception as conv_err:
            logger.error(f"[TTS] Error general al convertir a mu-law: {conv_err}")
            return b""
        
       
        return mulaw_data

    except Exception as e:
        # Este try-except captura errores de la llamada a ElevenLabs o problemas muy generales
        logger.error(f"[TTS] Error mayor en text_to_speech (ej. ElevenLabs API): {str(e)}")
        return b""
    






class RealTimeStreamProcessor:
    def __init__(self):
        self.reset()
        
    def reset(self):
        self.audio_buffer = bytearray()
        self.header_found = False
        self.wav_format = None
        self.bytes_per_sample = 2
        self.sample_rate = 24000
        self.channels = 1
        self.min_chunk_size = 480
        self.data_chunk_start = None
        self.total_received = 0
        
    def process_chunk_realtime(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self.total_received += len(chunk)
        self.audio_buffer.extend(chunk)
        if not self.header_found and len(self.audio_buffer) >= 44:
            self._detect_format()
        if not self.header_found and len(self.audio_buffer) > 2048:
            logger.info("[REALTIME] Asumiendo raw audio")
            self.wav_format = "raw"
            self.header_found = True
            self.data_chunk_start = 0
        if self.header_found:
            return self._extract_audio_chunks()
        return []

    def _detect_format(self):
        try:
            buffer_bytes = bytes(self.audio_buffer)
            if buffer_bytes.startswith(b'RIFF') and b'WAVE' in buffer_bytes[:20]:
                self.wav_format = "wav"
                self._parse_wav_header(buffer_bytes)
            else:
                self.wav_format = "raw"
                self.data_chunk_start = 0
            self.header_found = True
        except Exception as e:
            logger.warning(f"[REALTIME] Error detectando formato: {e}")

    def _parse_wav_header(self, data: bytes):
        try:
            pos = 12
            while pos < len(data) - 8:
                chunk_id = data[pos:pos+4]
                chunk_size = struct.unpack('<I', data[pos+4:pos+8])[0]
                if chunk_id == b'fmt ':
                    fmt_data = data[pos+8:pos+8+min(chunk_size, 16)]
                    if len(fmt_data) >= 16:
                        _, self.channels, self.sample_rate, _, _, bits = struct.unpack('<HHIIHH', fmt_data)
                        self.bytes_per_sample = bits // 8
                elif chunk_id == b'data':
                    self.data_chunk_start = pos + 8
                    break
                pos += 8 + chunk_size
        except Exception as e:
            logger.error(f"[REALTIME] Error parseando WAV header: {e}")
            self.wav_format = "raw"
            self.data_chunk_start = 0

    def _extract_audio_chunks(self) -> list[bytes]:
        chunks_ready = []
        if self.data_chunk_start is None:
            return chunks_ready
        try:
            if self.wav_format == "raw":
                available_audio = bytes(self.audio_buffer)
                remaining_header = b''
            else:
                if len(self.audio_buffer) <= self.data_chunk_start:
                    return chunks_ready
                available_audio = bytes(self.audio_buffer[self.data_chunk_start:])
                remaining_header = bytes(self.audio_buffer[:self.data_chunk_start])
            while len(available_audio) >= self.min_chunk_size:
                chunk_audio = available_audio[:self.min_chunk_size]
                available_audio = available_audio[self.min_chunk_size:]
                wav_chunk = self._create_mini_wav(chunk_audio)
                if wav_chunk:
                    chunks_ready.append(wav_chunk)
            if self.wav_format == "raw":
                self.audio_buffer = bytearray(available_audio)
            else:
                self.audio_buffer = bytearray(remaining_header + available_audio)
        except Exception as e:
            logger.error(f"[REALTIME] Error extrayendo chunks: {e}")
        return chunks_ready

    def _create_mini_wav(self, audio_data: bytes) -> Optional[bytes]:
        try:
            wav_buffer = io.BytesIO()
            data_size = len(audio_data)
            file_size = 36 + data_size
            wav_buffer.write(b'RIFF')
            wav_buffer.write(struct.pack('<I', file_size))
            wav_buffer.write(b'WAVE')
            wav_buffer.write(b'fmt ')
            wav_buffer.write(struct.pack('<I', 16))
            wav_buffer.write(struct.pack('<H', 1))
            wav_buffer.write(struct.pack('<H', self.channels))
            wav_buffer.write(struct.pack('<I', self.sample_rate))
            wav_buffer.write(struct.pack('<I', self.sample_rate * self.channels * self.bytes_per_sample))
            wav_buffer.write(struct.pack('<H', self.channels * self.bytes_per_sample))
            wav_buffer.write(struct.pack('<H', self.bytes_per_sample * 8))
            wav_buffer.write(b'data')
            wav_buffer.write(struct.pack('<I', data_size))
            wav_buffer.write(audio_data)
            return wav_buffer.getvalue()
        except Exception as e:
            logger.error(f"[REALTIME] Error creando WAV: {e}")
            return None

    def finalize(self) -> list[bytes]:
        if len(self.audio_buffer) > 0:
            return self._extract_audio_chunks()
        return []

realtime_processor = RealTimeStreamProcessor()

async def send_audio_to_twilio_realtime(audio_data: bytes, websocket_send: Callable, stream_sid: str):
    audio_b64 = base64.b64encode(audio_data).decode('utf-8')
    message = {
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": audio_b64
        }
    }
    websocket_send(json.dumps(message))

async def stream_tts_realtime_to_twilio(
    text: str,
    voice_id: str,
    api_key: str,
    send_audio_callback,      # se mantiene por compatibilidad; no se usa
    websocket_send,
    stream_sid: str,
) -> None:
    """
    Envía TTS de ElevenLabs a Twilio en tiempo real (chunks ~10 ms).
    Se apoya en `realtime_processor` y usa `send_audio_to_twilio_realtime`
    para empujar cada mini-WAV al WebSocket de Twilio.
    """
    if not api_key:
        api_key = ELEVEN_LABS_API_KEY  # respaldo

    realtime_processor.reset()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "Accept": "audio/wav",
        "Content-Type": "application/json",
        "xi-api-key": api_key,
    }
    body = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "output_format": "pcm_24000",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=60.0)) as cli:
        async with cli.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                logger.error(f"[RT-TTS] ElevenLabs error {resp.status_code}")
                return

            async for raw in resp.aiter_bytes(256):          # chunks muy pequeños
                for chunk in realtime_processor.process_chunk_realtime(raw):
                    await send_audio_to_twilio_realtime(chunk, websocket_send, stream_sid)

            # envía restos
            for chunk in realtime_processor.finalize():
                await send_audio_to_twilio_realtime(chunk, websocket_send, stream_sid)
