import os
from tts_utils import text_to_speech
from decouple import config

def main():
    # Texto de prueba para TTS
    test_text = "Hola, este es un test de TTS. Probando la generación de audio."

    # Llama a la función TTS para generar audio.
    audio_bytes = text_to_speech(test_text)

    # Definir ruta de salida para depuración (debe coincidir con lo que configuramos)
    output_path = os.path.join("audio", "respuesta_audio_debug.wav")

    if audio_bytes:
        print(f"Se generaron {len(audio_bytes)} bytes de audio.")
        print(f"El archivo debería haberse guardado en: {output_path}")
    else:
        print("Error: No se generó audio.")

if __name__ == "__main__":
    main()
