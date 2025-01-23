from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
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
        2. Genera el saludo en audio usando ElevenLabs.
        3. Responde con el audio generado.

    Respuesta:
        Siempre inicia con un saludo predefinido en audio generado por ElevenLabs.
    """
    try:
        # Parsear datos enviados por Twilio
        data = await request.form()
        print("Datos recibidos desde Twilio:", data)  # Log para depuración

        # Verificar si es el inicio de la llamada (sin SpeechResult)
        user_input = data.get("SpeechResult")
        if not user_input:
            # Mensaje predeterminado para inicio de la llamada
            response_text = "Buenas noches, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudar?"

            # Generar audio con ElevenLabs
            audio_url = generate_audio_with_eleven_labs(response_text)

            if not audio_url:
                # Si falla la generación de audio, responder con texto
                return Response(
                    content="<Response><Say>Hubo un problema al generar el audio. Por favor, intenta más tarde.</Say></Response>",
                    media_type="text/xml"
                )

            # Responder con el audio generado
            return Response(
                content=f"<Response><Play>{audio_url}</Play></Response>",
                media_type="text/xml"
            )

        # En caso de recibir un SpeechResult (ciclo posterior)
        greeting = "Buenas noches" if get_cancun_time().hour >= 18 else "Buen día"
        response_text = f"{greeting}, Consultorio del Doctor Wilfrido Alarcón. {user_input}. ¿En qué puedo ayudarte?"

        # Generar respuesta dinámica en audio
        audio_url = generate_audio_with_eleven_labs(response_text)

        if not audio_url:
            return Response(
                content="<Response><Say>Hubo un problema al generar la respuesta. Por favor, intenta más tarde.</Say></Response>",
                media_type="text/xml"
            )

        return Response(
            content=f"<Response><Play>{audio_url}</Play></Response>",
            media_type="text/xml"
        )

    except Exception as e:
        print("Error manejando la llamada desde Twilio:", e)
        return Response(
            content="<Response><Say>Hubo un error procesando tu solicitud.</Say></Response>",
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
            model="text-davinci-003",
            prompt=prompt,
            max_tokens=150,
            temperature=0.7,
        )
        return response["choices"][0]["text"].strip()
    except Exception as e:
        print("Error generando respuesta con OpenAI:", e)
        return "Lo siento, hubo un error procesando tu solicitud."







# **SECCIÓN 5: Generar audio con Eleven Labs**
def generate_audio_with_eleven_labs(text):
    """
    Convierte un texto en un archivo de audio usando Eleven Labs.

    Parámetros:
        text (str): Texto que se convertirá en audio.

    Configuración:
        - stability: Controla qué tan estable suena la voz (0.0 a 1.0).
        - similarity_boost: Mejora la emoción y consistencia (0.0 a 1.0).
        - speed: Velocidad del habla (0.5 a 2.0).
        - pitch: Tono de la voz (-2.0 a 2.0).

    NOTA:
        - Puedes ajustar estos parámetros para personalizar la voz según las necesidades.

    Retorna:
        str: URL del archivo de audio generado, o None si hay un error.
    """
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}"
        headers = {
            "xi-api-key": ELEVEN_LABS_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": 1.0,
                "pitch": 0.0
            }
        }
        response = requests.post(url, json=payload, headers=headers)

        # Log para depurar la respuesta
        print("Respuesta de Eleven Labs:", response.status_code, response.text)

        if response.status_code == 200:
            audio_url = response.json().get("audio_url")
            return audio_url
        else:
            return None
    except Exception as e:
        print("Error generando audio con Eleven Labs:", e)
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
