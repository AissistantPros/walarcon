from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from decouple import config
from utils import get_cancun_time
from datetime import datetime
from prompt import generate_openai_prompt
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from buscarslot import find_next_available_slot
from consultarinfo import read_sheet_data
import requests
import openai
from elevenlabs import ElevenLabs, VoiceSettings
import os
import io

# **SECCIÓN 1: Configuración del sistema**
CHATGPT_SECRET_KEY = config("CHATGPT_SECRET_KEY")
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = config("TWILIO_PHONE")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

# Inicializamos la aplicación FastAPI
app = FastAPI()

# **SECCIÓN 2: Endpoint raíz**
@app.get("/")
def read_root():
    """
    Ruta raíz para verificar que el servicio está activo.
    """
    return {"message": "El servicio está funcionando correctamente"}






# **SECCIÓN 3: Manejar llamadas desde Twilio**
@app.post("/twilio-call")
async def handle_call(request: Request):
    """
    Maneja las solicitudes de Twilio para procesar llamadas.

    Flujo:
        1. Si no hay input del usuario (inicio de la llamada), responde con un saludo predeterminado.
        2. Permite la conversación ida y vuelta con respuestas dinámicas.
        3. Detecta palabras clave para terminar la llamada de manera natural.
    """
    try:
        # Parsear datos enviados por Twilio
        data = await request.form()
        print("Datos recibidos desde Twilio:", data)

        # Variables clave de Twilio
        user_input = data.get("SpeechResult", "").lower()
        call_status = data.get("CallStatus", "active").lower()

        # Saludo inicial si no hay entrada del usuario
        if not user_input and call_status == "active":
            response_text = "Buenas noches, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudar?"

            # Generar audio con ElevenLabs
            audio_buffer = generate_audio_with_eleven_labs(response_text)
            if not audio_buffer:
                return Response(
                    content="<Response><Say>No puedo procesar tu solicitud en este momento.</Say></Response>",
                    media_type="text/xml"
                )

            return Response(
                content=f"<Response><Play>https://{TWILIO_PHONE}/audio.mp3</Play></Response>",
                media_type="text/xml"
            )

        # Detectar palabras clave para finalizar la llamada
        if any(keyword in user_input for keyword in ["adiós", "hasta luego", "gracias, eso es todo"]):
            farewell_text = "Gracias por llamar al consultorio del Doctor Wilfrido Alarcón. Que tenga una excelente noche."
            audio_buffer = generate_audio_with_eleven_labs(farewell_text)
            if not audio_buffer:
                return Response(
                    content="<Response><Say>Gracias por llamar. Hasta luego.</Say><Pause length='5'/></Response>",
                    media_type="text/xml"
                )
            return Response(
                content=f"<Response><Play>https://{TWILIO_PHONE}/audio.mp3</Play><Pause length='5'/></Response>",
                media_type="text/xml"
            )

        # Respuesta dinámica durante la conversación
        current_time = get_cancun_time()
        greeting = "Buenas noches" if current_time.hour >= 18 else "Buen día"
        response_text = f"{greeting}. Mencionaste: {user_input}. ¿Cómo más puedo ayudarte?"

        # Generar respuesta dinámica en audio
        audio_buffer = generate_audio_with_eleven_labs(response_text)
        if not audio_buffer:
            return Response(
                content="<Response><Say>No puedo procesar tu solicitud en este momento.</Say></Response>",
                media_type="text/xml"
            )

        return Response(
            content=f"<Response><Play>https://{TWILIO_PHONE}/audio.mp3</Play></Response>",
            media_type="text/xml"
        )

    except Exception as e:
        print("Error manejando la llamada desde Twilio:", e)
        return Response(
            content="<Response><Say>Ocurrió un error. Por favor, intenta de nuevo más tarde.</Say></Response>",
            media_type="text/xml"
        )






# **SECCIÓN 4: Modulo de OpenAI**
def generate_openai_response(prompt):
    """
    Genera una respuesta usando OpenAI a partir de un prompt.

    Parámetros:
        prompt (str): Prompt o mensaje de entrada para OpenAI.

    Retorna:
        str: Respuesta generada por OpenAI.
    """
    try:
        openai.api_key = CHATGPT_SECRET_KEY
        response = openai.Completion.create(
            model="gpt-4o",
            prompt=prompt,
            max_tokens=400,
            temperature=0.7,
        )
        return response["choices"][0]["text"].strip()
    except Exception as e:
        print("Error generando respuesta con OpenAI:", e)
        return "Lo siento, hubo un error procesando tu solicitud."







# **SECCIÓN 5: Generar audio con Eleven Labs**
# Configura tu API Key de ElevenLabs
api_key = ELEVEN_LABS_API_KEY
client = ElevenLabs(api_key=api_key)  # Inicialización del cliente global

# Función para generar audio con ElevenLabs
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
        # Configura los parámetros de la voz
        audio_data = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(stability=0.7, similarity_boost=0.85)
        )

        # Guarda el audio en un buffer en memoria
        audio_buffer = io.BytesIO()
        for chunk in audio_data:
            audio_buffer.write(chunk)
        audio_buffer.seek(0)  # Regresa al inicio del archivo

        print("Audio generado en memoria.")
        return audio_buffer

    except Exception as e:
        print(f"Error al generar audio con ElevenLabs: {e}")
        return None
 












# **SECCIÓN 6: Endpoint para consultar información desde Google Sheets**
@app.get("/consultar-informacion")
def consultar_informacion():
    """
    Endpoint para leer y devolver datos desde Google Sheets.
    """
    try:
        data = read_sheet_data()
        return JSONResponse({"message": "Información obtenida con éxito", "data": data})
    except Exception as e:
        return JSONResponse({"error": "Error al consultar la información", "details": str(e)}, status_code=500)



# **SECCIÓN 7: Endpoint para buscar el próximo slot disponible**
@app.get("/buscar-slot")
def buscar_slot():
    """
    Endpoint para buscar el próximo horario disponible en Google Calendar.
    """
    try:
        slot = find_next_available_slot()
        if slot:
            return JSONResponse({"message": "Slot disponible encontrado", "slot": slot})
        else:
            return JSONResponse({"message": "No se encontraron horarios disponibles"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": "Error al buscar el slot disponible"}, status_code=500)


# **SECCIÓN 8: Endpoint para crear citas**
@app.post("/crear-cita")
async def crear_cita(request: Request):
    """
    Endpoint para crear una nueva cita en Google Calendar.
    """
    try:
        # Obtener datos enviados en la solicitud
        data = await request.json()
        print("Datos recibidos:", data)  # LOG: Mostrar los datos recibidos en los logs

        name = data.get("name")
        phone = data.get("phone")
        reason = data.get("reason", "No especificado")
        start_time = data.get("start_time")
        end_time = data.get("end_time")

        # Validar campos obligatorios
        if not name or not phone:
            raise ValueError("Los campos 'name' y 'phone' son obligatorios.")
        if len(phone) != 10 or not phone.isdigit():
            raise ValueError("El campo 'phone' debe ser un número de 10 dígitos.")
        if not start_time or not end_time:
            raise ValueError("Los campos 'start_time' y 'end_time' son obligatorios y deben estar en formato ISO.")

        # Convertir fechas a formato datetime
        start_time = datetime.fromisoformat(start_time)
        end_time = datetime.fromisoformat(end_time)

        print("Creando evento con:", name, phone, reason, start_time, end_time)  # LOG: Mostrar datos procesados

        # Crear cita en Google Calendar
        event = create_calendar_event(name, phone, reason, start_time, end_time)
        return JSONResponse({"message": "Cita creada con éxito", "event": event})
    except ValueError as e:
        print("Error de validación:", e)  # LOG: Mostrar errores de validación
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        print("Error al crear la cita:", e)  # LOG: Mostrar el error exacto
        return JSONResponse({"error": "Error al crear la cita", "details": str(e)}, status_code=500)

# **SECCIÓN 9: Endpoint para editar citas**
@app.put("/editar-cita")
async def editar_cita(request: Request):
    """
    Endpoint para editar una cita existente en Google Calendar.
    """
    try:
        data = await request.json()
        phone = data.get("phone")
        original_start_time = datetime.fromisoformat(data.get("original_start_time"))
        new_start_time = data.get("new_start_time")
        new_end_time = data.get("new_end_time")

        if new_start_time and new_end_time:
            new_start_time = datetime.fromisoformat(new_start_time)
            new_end_time = datetime.fromisoformat(new_end_time)

        # Editar la cita
        result = edit_calendar_event(phone, original_start_time, new_start_time, new_end_time)
        return JSONResponse({"message": "Cita editada con éxito", "result": result})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "Error al editar la cita"}, status_code=500)

# **SECCIÓN 10: Endpoint para eliminar citas**
@app.delete("/eliminar-cita")
async def eliminar_cita(request: Request):
    """
    Endpoint para eliminar una cita existente en Google Calendar.
    """
    try:
        data = await request.json()
        phone = data.get("phone")
        patient_name = data.get("patient_name", None)

        # Eliminar cita
        result = delete_calendar_event(phone, patient_name)
        return JSONResponse({"message": "Cita eliminada con éxito", "result": result})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "Error al eliminar la cita"}, status_code=500)
