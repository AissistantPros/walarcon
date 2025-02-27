# audio_in.py

import time

class AudioBuffer:
    """
    Esta clase maneja la lógica de:
    1. Acumular audio crudo (MULAW 8kHz) que llega por chunks.
    2. Detectar silencio de ~2 segundos (fin de bloque).
    3. Forzar corte a los ~10 segundos de audio continuo (bloque máximo).
    """

    def __init__(self, silence_threshold: int = 500):
        """
        :param silence_threshold: Umbral de 'energía' o 'volumen' para decidir si es silencio.
        """
        self.buffer = bytearray()
        self.start_time = time.time()          # Momento en que inicia el bloque actual
        self.last_voice_time = time.time()     # Última vez que detectamos 'voz'
        self.silence_threshold = silence_threshold

    def process_chunk(self, data: bytes):
        """
        Procesa un chunk de audio en formato MULAW 8kHz.
        
        :param data: Datos de audio en bytes.
        :return: 
          - Si se detecta que terminó un bloque (2s de silencio o 10s totales),
            regresa el bloque (bytes) y reinicia el buffer.
          - De lo contrario, regresa None (significa que seguimos en el mismo bloque).
        """

        # 1. Verificar si este chunk contiene "voz"
        if self._has_voice(data):
            self.last_voice_time = time.time()

        # 2. Acumular el chunk en el buffer actual
        self.buffer.extend(data)

        # 3. Medir tiempo transcurrido desde el inicio del bloque
        elapsed_since_start = time.time() - self.start_time

        # 4. Medir tiempo desde la última voz
        elapsed_since_voice = time.time() - self.last_voice_time

        # 5. Verificar condiciones de corte
        #    a) 2s de silencio
        #    b) 10s de duración total
        if elapsed_since_voice >= 2.0 or elapsed_since_start >= 10.0:
            # Bloque completado
            completed_block = bytes(self.buffer)

            # Reiniciar buffer y tiempos
            self.buffer.clear()
            self.start_time = time.time()
            self.last_voice_time = time.time()

            # Devolver el bloque completado
            return completed_block

        # Si no se ha cumplido ninguna condición, seguimos
        return None

    def _has_voice(self, data: bytes) -> bool:
        """
        Estima si hay 'voz' en el chunk basándose en un cálculo muy simple 
        de 'energía' en formato MULAW.

        *Nota:* Esto es muy rudimentario y puede mejorarse con librerías 
        como webrtcvad, pero nos sirve de ejemplo.
        """

        total_energy = 0
        for b in data:
            # MuLaw offset: el rango es de 0 a 255
            # Normalizamos en [-128, 127]
            sample_val = b - 128
            total_energy += abs(sample_val)

        # Energía promedio de este chunk
        if len(data) > 0:
            avg_energy = total_energy / len(data)
        else:
            avg_energy = 0

        # Si la energía promedio supera el threshold, consideramos que hay voz
        return avg_energy > self.silence_threshold


# ============================================================================
# Prueba local (opcional): Generar datos aleatorios para ver si corta bloques
# ============================================================================
if __name__ == "__main__":
    import random

    audio_buffer = AudioBuffer(silence_threshold=50)
    print("Iniciando prueba local de AudioBuffer (sin Twilio).")

    for i in range(100):
        # Simular un chunk de 160 bytes (20 ms a 8kHz)
        simulated_chunk = bytearray()
        for _ in range(160):
            # 15% probabilidad de 'voz' (bytes alrededor de 200).
            if random.random() < 0.15:
                simulated_chunk.append(random.randint(180, 220))
            else:
                # Valor más cercano a 128 (silencio)
                simulated_chunk.append(random.randint(120, 136))

        block = audio_buffer.process_chunk(simulated_chunk)
        if block:
            print("=== BLOQUE COMPLETO DETECTADO ===")
            print("Tamaño del bloque:", len(block), "bytes")
            # Podrías guardar el block en un archivo .ulaw si deseas
            # pero aquí solo imprimimos la detección
        time.sleep(0.02)  # Simular pausa de 20ms entre chunks
