# -*- coding: utf-8 -*-
#utils.py
"""
Módulo de utilidades para integración con Google APIs y manejo de tiempo.
"""

import base64
import os
import json
import logging, asyncio
import threading
from datetime import datetime, timedelta, date # Asegúrate que 'date' está aquí
import httpx
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import re
from typing import Dict, Optional, List, Any # Añadido Any y List
from state_store import session_state
from twilio.rest import Client
import time

      
logger = logging.getLogger("utils")

ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
_twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN)



# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------
# 🔐 Variables de Entorno (NO modificar nombres)
# ------------------------------------------
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_SHEET_ID = config("GOOGLE_SHEET_ID")  # ✅ Nombre exacto
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_PRIVATE_KEY_ID = config("GOOGLE_PRIVATE_KEY_ID")
GOOGLE_CLIENT_ID = config("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_CERT_URL = config("GOOGLE_CLIENT_CERT_URL")





# -----------------------------------------------------------------------------
# FUNCIONES DE UTILIDAD PARA FORMATEO DE FECHA Y HORA (Centralizadas aquí)
# -----------------------------------------------------------------------------

NUMEROS_A_PALABRAS: Dict[int, str] = {
    0: "en punto", 1: "una", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco",
    6: "seis", 7: "siete", 8: "ocho", 9: "nueve", 10: "diez",
    11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
    16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
    20: "veinte", 21: "veintiuno", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro",
    25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve",
    30: "treinta", 40: "cuarenta", 45: "cuarenta y cinco", 50: "cincuenta",
    # Para minutos específicos no listados, la función puede construir "treinta y uno", "cuarenta y dos", etc.
    # O puedes añadir más aquí si prefieres, ej: 31: "treinta y una" (si es la hora)
}

def convertir_hora_a_palabras(hhmm_str: str) -> str:
    """
    Convierte una cadena de hora "HH:MM" a un formato en palabras más natural
    para TTS, incluyendo "de la mañana", "de la tarde", etc.
    Ej: "09:30" -> "nueve treinta de la mañana"
        "12:15" -> "doce quince del mediodía"
        "14:00" -> "dos en punto de la tarde"
    """
    try:
        h, m = map(int, hhmm_str.split(':'))

        sufijo_horario = "de la mañana" # Valor por defecto
        display_h = h

        if h == 0: # Medianoche
            display_h = 12
            sufijo_horario = "de la madrugada" # o "de la noche"
        elif h == 12: # Mediodía
            sufijo_horario = "del mediodía"
        elif 13 <= h <= 17: # Tarde (1 PM a 5 PM)
            display_h = h - 12
            sufijo_horario = "de la tarde"
        elif h >= 18: # Tarde-Noche (6 PM en adelante)
            display_h = h - 12
            sufijo_horario = "de la noche" # o "de la tarde-noche"
        # Horas AM (1-11) ya tienen display_h = h y sufijo_horario "de la mañana"

        hora_palabra = NUMEROS_A_PALABRAS.get(display_h, str(display_h))
        # Caso especial para "una" de la mañana/tarde en lugar de "uno"
        if display_h == 1 and (sufijo_horario == "de la mañana" or sufijo_horario == "de la tarde" or sufijo_horario == "de la madrugada"):
            hora_palabra = "una"


        minuto_palabra = ""
        if m == 0:
            minuto_palabra = "en punto"
        elif m == 15:
            minuto_palabra = "quince"
        elif m == 30:
            # Preferir "y media" excepto para las 12:30 ("doce y treinta")
            minuto_palabra = "y media" if display_h != 12 else "treinta"
        elif m == 45:
            minuto_palabra = "cuarenta y cinco"
        elif m < 30 : # Para minutos como 01-29 no cubiertos
            if m in NUMEROS_A_PALABRAS:
                minuto_palabra = NUMEROS_A_PALABRAS[m]
            elif m > 20: # veintiuno, veintidós...
                 minuto_palabra = f"veinti{NUMEROS_A_PALABRAS.get(m % 10, str(m % 10))}" if m % 10 != 0 else "veinte" # ej. veintidós
                 if m % 10 == 1 and m != 21: minuto_palabra = f"veintiún" # veintiún (antes de sustantivo) - no aplica aquí directamente
                 elif m == 21 : minuto_palabra = "veintiuno"

            else: # Para 1-19 no especiales
                minuto_palabra = str(m) # Fallback
        elif m > 30: # Para minutos como 31-59 no cubiertos
            decena = (m // 10) * 10
            unidad = m % 10
            if unidad == 0: # cuarenta, cincuenta
                minuto_palabra = NUMEROS_A_PALABRAS.get(decena, str(decena))
            else: # treinta y uno, cuarenta y dos...
                palabra_decena = NUMEROS_A_PALABRAS.get(decena, str(decena))
                palabra_unidad = NUMEROS_A_PALABRAS.get(unidad, str(unidad))
                minuto_palabra = f"{palabra_decena} y {palabra_unidad}"
        else: # Fallback si algo se escapa
            minuto_palabra = str(m)

        # Ajustes finales para frases comunes
        if minuto_palabra == "en punto":
            return f"las {hora_palabra} {minuto_palabra} {sufijo_horario}"
        if minuto_palabra == "y media":
             return f"las {hora_palabra} y media {sufijo_horario}"

        return f"las {hora_palabra} {minuto_palabra} {sufijo_horario}"

    except Exception as e:
        logger.error(f"Error convirtiendo hora '{hhmm_str}' a palabras: {e}")
        # Fallback a una representación numérica simple si falla la conversión a palabras
        try:
            t_obj = datetime.strptime(hhmm_str, "%H:%M")
            suf = "de la mañana" if t_obj.hour < 12 else "de la tarde"
            if t_obj.hour == 12: suf = "del mediodía"
            elif t_obj.hour == 0: suf = "de la madrugada"
            
            hora_fmt = t_obj.strftime("%I").lstrip("0")
            min_fmt = t_obj.strftime("%M")
            return f"las {hora_fmt}:{min_fmt} {suf}"
        except: # Fallback absoluto
            return hhmm_str


DAYS_EN_TO_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado",
    "Sunday": "Domingo",
}
MONTHS_EN_TO_ES = {
    "January": "Enero", "February": "Febrero", "March": "Marzo", "April": "Abril",
    "May": "Mayo", "June": "Junio", "July": "Julio", "August": "Agosto",
    "September": "Septiembre", "October": "Octubre", "November": "Noviembre", "December": "Diciembre",
}

def format_date_nicely(target_date_obj: date, time_keyword: Optional[str] = None,
                       weekday_override: Optional[str] = None,
                       specific_time_hhmm: Optional[str] = None) -> str:
    """
    Formatea una fecha y opcionalmente una hora en una cadena amigable para el usuario.
    Usa convertir_hora_a_palabras para la parte de la hora.
    """
    day_name_es = DAYS_EN_TO_ES.get(target_date_obj.strftime("%A"), target_date_obj.strftime("%A"))
    if weekday_override:
        day_name_es = weekday_override.capitalize()
    month_es = MONTHS_EN_TO_ES.get(target_date_obj.strftime("%B"), target_date_obj.strftime("%B"))
    text = f"{day_name_es} {target_date_obj.day} de {month_es}"

    if specific_time_hhmm: # specific_time_hhmm es una cadena como "09:30"
        try:
            hora_en_palabras = convertir_hora_a_palabras(specific_time_hhmm)
            text += f" a {hora_en_palabras}" # "a las nueve treinta de la mañana"
        except Exception as e:
            logger.warning(f"Fallback en format_date_nicely para la hora '{specific_time_hhmm}': {e}")
            # Fallback si convertir_hora_a_palabras tiene problemas, aunque no debería si está bien probada
            text += f" a las {specific_time_hhmm}"
    elif time_keyword == "mañana":
        text += ", por la mañana"
    elif time_keyword == "tarde":
        text += ", por la tarde"
    return text




def initialize_google_calendar():
    """Inicializa el servicio de Google Calendar."""
    try:
        logger.info("🔍 Inicializando Google Calendar...")
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": GOOGLE_CLIENT_CERT_URL
        }
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        raise



def initialize_google_sheets():
    """Inicializa el servicio de Google Sheets."""
    try:
        logger.info("🔍 Inicializando Google Sheets...")
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": GOOGLE_CLIENT_CERT_URL
        }
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        service.sheet_id = GOOGLE_SHEET_ID  # Adjuntamos el ID para uso futuro
        return service
    except Exception as e:
        logger.error(f"❌ Error en Google Sheets: {str(e)}")
        raise





def get_cancun_time():
    """Obtiene la hora actual en Cancún."""
    return datetime.now(pytz.timezone("America/Cancun"))



def convert_utc_to_cancun(utc_str):
    """Convierte un string UTC (ISO8601) a datetime en zona horaria de Cancún."""
    from datetime import datetime
    import pytz

    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    cancun_tz = pytz.timezone("America/Cancun")
    return utc_dt.astimezone(cancun_tz)





# -----------------------------------------------------------------------------
# FUNCIÓN search_calendar_event_by_phone (MODIFICADA)
# -----------------------------------------------------------------------------
def search_calendar_event_by_phone(phone: str) -> List[Dict[str, Any]]:
    """
    Busca citas por número de teléfono y devuelve una lista de diccionarios
    con una estructura clara para la IA.
    """
    logger.info(f"Iniciando búsqueda de citas para el teléfono: {phone}")
    try:
        service = initialize_google_calendar()
        # Google Calendar API espera la hora en UTC para timeMin
        # Usamos la fecha actual de Cancún, convertida a inicio del día en UTC para no perder eventos del día
        now_cancun = get_cancun_time()
        start_of_today_cancun = now_cancun.replace(hour=0, minute=0, second=0, microsecond=0)
        time_min_utc_iso = start_of_today_cancun.astimezone(pytz.utc).isoformat()

        logger.debug(f"Buscando eventos en Google Calendar para el teléfono: {phone} desde {time_min_utc_iso} (UTC).")

        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,
            timeMin=time_min_utc_iso, # Solo citas desde el inicio del día de hoy (en UTC) hacia adelante
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        
        items = events_result.get("items", [])
        logger.info(f"Google Calendar API encontró {len(items)} eventos crudos para el teléfono {phone}.")

        parsed_events: List[Dict[str, Any]] = []
        for evt_idx, evt in enumerate(items):
            logger.debug(f"Procesando evento crudo #{evt_idx + 1}: ID {evt.get('id')}, Summary: {evt.get('summary')}")
            summary = evt.get("summary", "Paciente Desconocido")
            description = evt.get("description", "")
            
            motive = None
            phone_in_desc = None
            
            lines = description.split("\n")
            for line in lines:
                line_lower = line.lower()
                # Búsqueda más robusta para teléfono y motivo
                if re.search(r"tel[eé]fono\s*:", line_lower):
                    phone_in_desc = re.sub(r"[^\d\s\+\-\(\)]", "", line.split(":", 1)[-1]).strip() # Limpia un poco más
                    phone_in_desc = re.sub(r"\s+", "", phone_in_desc) # Quita espacios internos
                if re.search(r"motivo\s*:", line_lower):
                    motive = line.split(":", 1)[-1].strip()

            start_utc_str = evt.get("start", {}).get("dateTime")
            # end_utc_str = evt.get("end", {}).get("dateTime") # No se usa en el dict de salida actualmente

            start_cancun_dt_obj: Optional[datetime] = None
            start_cancun_pretty_str: str = "Fecha/hora no disponible"
            start_cancun_iso_for_tool_str: Optional[str] = None

            if start_utc_str:
                try:
                    start_cancun_dt_obj = convert_utc_to_cancun(start_utc_str)
                    start_cancun_iso_for_tool_str = start_cancun_dt_obj.isoformat()
                    start_cancun_pretty_str = format_date_nicely(
                        start_cancun_dt_obj.date(), 
                        specific_time_hhmm=start_cancun_dt_obj.strftime("%H:%M")
                    )
                except Exception as e_conv:
                    logger.error(f"Error convirtiendo/formateando fecha para evento ID {evt.get('id')}, start_utc_str '{start_utc_str}': {e_conv}")
            else:
                logger.warning(f"Evento ID {evt.get('id')} no tiene start.dateTime.")


            cita_parseada = {
                "event_id": evt.get("id"), # ID real de Google Calendar
                "patient_name": summary,   # Nombre del paciente (del campo summary de Google)
                "start_time_iso_utc": start_utc_str, # Hora de inicio original en UTC
                "start_time_cancun_iso": start_cancun_iso_for_tool_str, # Hora de inicio en Cancún ISO (para herramientas)
                "start_time_cancun_pretty": start_cancun_pretty_str, # Hora de inicio formateada (para leer al usuario)
                "appointment_reason": motive if motive else "No especificado", # Motivo extraído
                "phone_in_description": phone_in_desc # Teléfono de la descripción
            }
            # Guarda el ID real para que la tool de borrado lo use si GPT manda un placeholder
            session_state["last_event_found"] = cita_parseada["event_id"]


            parsed_events.append(cita_parseada)
            logger.debug(f"Evento parseado y añadido: {cita_parseada}")

            
            # Guardar la lista completa y un ID por defecto en la memoria de la llamada
            session_state["events_found"] = parsed_events
            if parsed_events:
                session_state["current_event_id"] = parsed_events[0]["event_id"]  # la primera por defecto
           

        if not parsed_events:
            logger.info(f"No se encontraron citas parseables para el teléfono {phone} que cumplan los criterios.")
        else:
            logger.info(f"Se parsearon {len(parsed_events)} citas para el teléfono {phone}.")
            
        return parsed_events

    except Exception as e:
        logger.error(f"❌ Error general en search_calendar_event_by_phone para el teléfono {phone}: {str(e)}", exc_info=True)
        return [] # Devolver lista vacía en caso de error mayor












async def terminar_llamada_twilio(call_sid: str, motivo: str = "completed"):
    """
    ☎️ Termina la llamada en Twilio usando la API REST
    
    Args:
        call_sid: ID de la llamada en Twilio
        motivo: Razón de la terminación (default: "completed")
    
    Returns:
        bool: True si se terminó exitosamente, False si hubo error
    """
    logger.info(f"☎️ Terminando llamada {call_sid} en Twilio (motivo={motivo})...")
    t0 = time.perf_counter()
    
    try:
        # Verificar que tenemos el cliente de Twilio
        if not _twilio_client:
            logger.error("❌ Cliente de Twilio no inicializado")
            return False
        
        # Verificar que el call_sid es válido
        if not call_sid or len(call_sid) < 10:
            logger.error(f"❌ Call SID inválido: {call_sid}")
            return False
        
        # Terminar la llamada usando la API REST
        await asyncio.to_thread(
            _twilio_client.calls(call_sid).update,
            status="completed"
        )
        
        logger.info(f"✅ Twilio confirmó cierre de llamada {call_sid}")
        logger.info(f"[LATENCIA] Terminación Twilio completada en {1000*(time.perf_counter()-t0):.1f} ms")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error terminando llamada {call_sid} en Twilio: {e}")
        logger.error(f"[LATENCIA] Error en terminación Twilio tras {1000*(time.perf_counter()-t0):.1f} ms")
        return False




async def cierre_con_despedida(manager, reason: str, delay: float = 5.0):
    """
    🔚 FLUJO ELEGANTE DE TERMINACIÓN DE LLAMADA
    
    Pasos:
    1. IA se despide (TTS)
    2. Espera para reproducción completa (5 segundos)
    3. Cierra WebSockets (Deepgram, ElevenLabs)
    4. Termina llamada en Twilio (API REST)
    5. Limpia memoria, cancela tareas, borra buffers
    
    Args:
        manager: CallOrchestrator instance
        reason: Razón de la terminación
        delay: Tiempo de espera para reproducción (default: 5.0s)
    """
    FAREWELL = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"
    
    logger.info(f"🔚 Iniciando cierre elegante de llamada - Razón: {reason}")
    t0 = time.perf_counter()
    
    try:
        # === PASO 1: DESPEDIDA TTS ===
        logger.info("🎤 Enviando despedida TTS...")
        
        # Activar modo "ignorar STT" inmediatamente
        if hasattr(manager, 'audio_manager') and manager.audio_manager:
            manager.audio_manager.state.ignore_stt = True
            manager.audio_manager.state.is_speaking = True
            logger.info("🤫 Activando ignorar_stt para la secuencia de cierre")
        
        # Enviar despedida
        if hasattr(manager, '_handle_ai_response'):
            await manager._handle_ai_response(FAREWELL)
            logger.info("✅ Despedida TTS enviada")
        else:
            logger.warning("⚠️ No se pudo enviar despedida TTS")
        
        # === PASO 2: ESPERA PARA REPRODUCCIÓN ===
        logger.info(f"⏳ Esperando {delay}s para reproducción completa...")
        await asyncio.sleep(delay)
        
        # === PASO 3: CIERRE ELEGANTE DE WEBSOCKETS ===
        logger.info("🔌 Cerrando WebSockets...")
        
        # Cerrar AudioManager (Deepgram + ElevenLabs)
        if hasattr(manager, 'audio_manager') and manager.audio_manager:
            try:
                await manager.audio_manager.shutdown()
                logger.info("✅ AudioManager cerrado")
            except Exception as e:
                logger.error(f"❌ Error cerrando AudioManager: {e}")
        
        # Cerrar ConversationFlow
        if hasattr(manager, 'conversation_flow') and manager.conversation_flow:
            try:
                await manager.conversation_flow.shutdown()
                logger.info("✅ ConversationFlow cerrado")
            except Exception as e:
                logger.error(f"❌ Error cerrando ConversationFlow: {e}")
        
        # Cerrar IntegrationManager
        if hasattr(manager, 'integration_manager') and manager.integration_manager:
            try:
                await manager.integration_manager.shutdown()
                logger.info("✅ IntegrationManager cerrado")
            except Exception as e:
                logger.error(f"❌ Error cerrando IntegrationManager: {e}")
        
        # === PASO 4: TERMINACIÓN EN TWILIO ===
        logger.info("☎️ Terminando llamada en Twilio...")
        
        if hasattr(manager, 'call_state') and manager.call_state.call_sid:
            try:
                await terminar_llamada_twilio(manager.call_state.call_sid, reason)
                manager.call_state.twilio_terminated = True
                logger.info("✅ Llamada terminada en Twilio")
            except Exception as e:
                logger.error(f"❌ Error terminando llamada en Twilio: {e}")
        else:
            logger.warning("⚠️ Sin call_sid; no se pudo terminar en Twilio")
        
        # === PASO 5: LIMPIEZA COMPLETA ===
        logger.info("🧹 Limpieza final de memoria y tareas...")
        
        # Cancelar tareas de monitoreo
        if hasattr(manager, 'monitor_task') and manager.monitor_task:
            try:
                manager.monitor_task.cancel()
                logger.info("✅ Tarea de monitoreo cancelada")
            except Exception as e:
                logger.error(f"❌ Error cancelando tarea de monitoreo: {e}")
        
        # Limpiar session_state
        try:
            session_state.clear()
            logger.info("✅ Session state limpiado")
        except Exception as e:
            logger.error(f"❌ Error limpiando session state: {e}")
        
        # Marcar llamada como terminada
        if hasattr(manager, 'call_state'):
            manager.call_state.ended = True
            manager.call_state.ending_reason = reason
        
        logger.info(f"✅ Cierre elegante completado en {1000*(time.perf_counter()-t0):.1f} ms")
        
    except Exception as e:
        logger.error(f"❌ Error en cierre elegante: {e}")
        # Intentar shutdown de emergencia
        try:
            if hasattr(manager, '_shutdown'):
                await manager._shutdown(f"emergency_shutdown ({reason})")
        except Exception as emergency_error:
            logger.error(f"❌ Error en shutdown de emergencia: {emergency_error}")
    finally:
        logger.info(f"🔚 Cierre de llamada finalizado - Razón: {reason}")


def normalizar_telefono(texto: str) -> str:
    """
    Normaliza un número de teléfono:
    - Convierte palabras numéricas a dígitos (soporta hasta 99).
    - Elimina cualquier carácter que no sea dígito.
    - Valida que el resultado tenga exactamente 10 dígitos.
    - Lanza ValueError si no es posible obtener un teléfono válido.
    """
    import re
    # Diccionario básico de palabras a números (puedes expandir según necesidad)
    palabras_a_digitos = {
        "cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8", "nueve": "9",
        "diez": "10", "once": "11", "doce": "12", "trece": "13", "catorce": "14", "quince": "15", "dieciséis": "16", "dieciseis": "16", "diecisiete": "17", "dieciocho": "18", "diecinueve": "19",
        "veinte": "20", "veintiuno": "21", "veintidós": "22", "veintidos": "22", "veintitrés": "23", "veintitres": "23", "veinticuatro": "24", "veinticinco": "25", "veintiséis": "26", "veintiseis": "26", "veintisiete": "27", "veintiocho": "28", "veintinueve": "29",
        "treinta": "30", "cuarenta": "40", "cincuenta": "50", "sesenta": "60", "setenta": "70", "ochenta": "80", "noventa": "90"
    }
    # Reemplaza palabras por dígitos
    texto = texto.lower()
    for palabra, digito in palabras_a_digitos.items():
        texto = re.sub(rf"\\b{palabra}\\b", digito, texto)
    # Elimina todo lo que no sea dígito
    solo_digitos = re.sub(r"\D", "", texto)
    if len(solo_digitos) != 10:
        raise ValueError("El teléfono debe tener exactamente 10 dígitos después de normalizar. Recibido: " + solo_digitos)
    return solo_digitos