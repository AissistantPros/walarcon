from elevenlabs import ElevenLabs, VoiceSettings
import io
from decouple import config

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def generate_audio_with_eleven_labs(text):
    try:
        audio_stream = client.text_to_speech.convert(
            text=text,
            voice_id=ELEVEN_LABS_VOICE_ID,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(stability=0.7, similarity_boost=0.85)
        )

        buffer = io.BytesIO()
        for chunk in audio_stream:
            buffer.write(chunk)
        buffer.seek(0)
        return buffer
    except Exception as e:
        print(f"Error generando audio con ElevenLabs: {e}")
        return None
