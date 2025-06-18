#tts_utils.py
import time
# import wave # Puedes descomentar wave si necesitas la depuración de archivos WAV
import logging
import os
import audioop  # type: ignore # <-- Asegúrate de importar audioop
import httpx, asyncio
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings
import numpy as np


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
    

async def elevenlabs_ulaw_fragments(text: str,
                                    voice_id: str = ELEVEN_LABS_VOICE_ID,
                                    frag_size: int = 160,
                                    ms_per_chunk: int = 20):
    """
    Generador asíncrono que produce fragmentos µ-law (8 kHz, 8-bit) listos
    para Twilio Media Streams, ~20 ms cada uno.
    """
    if not ELEVEN_LABS_API_KEY:
        logger.error("[TTS-STREAM] Falta ELEVEN_LABS_API_KEY.")
        return

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
    headers = {
        "xi-api-key": ELEVEN_LABS_API_KEY,
        "accept": "audio/pcm",  # RAW PCM 16-bit, 8kHz mono
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.75,
            "style": 0.45,
            "use_speaker_boost": False,
            "speed": 1.2
        }
    }

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            buffer_pcm = b""

            async for pcm_chunk in resp.aiter_bytes():
                if not pcm_chunk:
                    continue

                buffer_pcm += pcm_chunk

                # Asegura múltiplos de 2 para 16-bit
                safe_len = len(buffer_pcm) - (len(buffer_pcm) % 2)
                if safe_len == 0:
                    continue

                safe_pcm = buffer_pcm[:safe_len]
                buffer_pcm = buffer_pcm[safe_len:]

                # 🔊 Amplificación segura con NumPy
                pcm_array = np.frombuffer(safe_pcm, dtype=np.int16)
                amplified = pcm_array.astype(np.float32) * 2.4  # 2.0–2.5 es razonable
                limited = np.clip(amplified, -32768, 32767).astype(np.int16)
                amplified_bytes = limited.tobytes()

                ulaw = audioop.lin2ulaw(amplified_bytes, 2)

                # 🧪 Guardar para debug (solo 1 vez)
                if not hasattr(elevenlabs_ulaw_fragments, "_debug_guardado"):
                    with open("debug_pcm.raw", "wb") as f_pcm:
                        f_pcm.write(safe_pcm[:1000])
                    with open("debug_ulaw.raw", "wb") as f_ulaw:
                        f_ulaw.write(ulaw[:1000])
                    elevenlabs_ulaw_fragments._debug_guardado = True
                    logger.warning("🧪 Guardado debug_pcm.raw y debug_ulaw.raw para inspección.")

                # Fragmentar y enviar
                for i in range(0, len(ulaw), frag_size):
                    yield ulaw[i:i + frag_size]

                await asyncio.sleep(ms_per_chunk / 1000)




def text_to_speech_sin_numpy(text: str) -> bytes:
    """
    Genera audio TTS desde ElevenLabs sin modificar el volumen ni usar NumPy.
    Solo convierte PCM 16-bit a μ-law y lo devuelve.
    """
    if not elevenlabs_client:
        return b""

    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=ELEVEN_LABS_VOICE_ID,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.75,
                style=0.45,
                use_speaker_boost=False,
                speed=1.2
            ),
            output_format="pcm_8000"
        )
        pcm_bytes = b"".join(audio_stream)

        if not pcm_bytes:
            logger.error("❌ ElevenLabs no devolvió audio PCM.")
            return b""

        # Solo conversión a μ-law
        mulaw_bytes = audioop.lin2ulaw(pcm_bytes, 2)
        return mulaw_bytes

    except Exception as e:
        logger.error(f"❌ Error en text_to_speech_sin_numpy: {e}", exc_info=True)
        return b""
