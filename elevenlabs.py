import io
from elevenlabs import ElevenLabs, VoiceSettings
from decouple import config

# **SECCIÓN: Configuración de Eleven Labs**
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

# Inicialización del cliente de ElevenLabs
client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def generate_audio_with_eleven_labs(text, voice_id=ELEVEN_LABS_VOICE_ID):
    """
    Genera un archivo de audio en memoria usando ElevenLabs.

    Args:
        text (str): Texto que se convertirá en audio.
        voice_id (str): ID de la voz de ElevenLabs.

    Returns:
        io.BytesIO: Archivo de audio en memoria o None si ocurre un error.
    """
    try:
        audio_data = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(stability=0.7, similarity_boost=0.85)
        )

        audio_buffer = io.BytesIO()
        for chunk in audio_data:
            audio_buffer.write(chunk)
        audio_buffer.seek(0)  # Regresa al inicio del archivo

        print("Audio generado en memoria.")
        return audio_buffer

    except Exception as e:
        print(f"Error al generar audio con ElevenLabs: {e}")
        return None
