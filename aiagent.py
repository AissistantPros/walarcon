# -*- coding: utf-8 -*-
"""
Módulo principal del agente de IA - Dr. Alarcón IVR System
Función principal: Procesar entradas de usuario y gestionar integraciones con APIs externas
"""

# ==================================================
# Parte 1: Configuración inicial y dependencias
# ==================================================
# Propósito: 
# - Importar librerías necesarias
# - Configurar logging
# - Inicializar cliente de OpenAI

import logging
import time
from decouple import config
from openai import OpenAI
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from prompt import generate_openai_prompt

# Configurar sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar cliente OpenAI
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))







# ==================================================
# Parte 2: Núcleo de generación de respuestas
# ==================================================
# Propósito:
# - Comunicarse con OpenAI
# - Ejecutar herramientas según sea necesario
# - Manejar errores de integraciones

def generate_openai_response(conversation_history: list):
    """
    Procesa la conversación y genera una respuesta usando GPT-4o
    """
    try:
        start_time = time.time()
        
        # Generar prompt con instrucciones específicas
        messages = generate_openai_prompt(conversation_history)
        
        # Configurar herramientas disponibles
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_sheet_data",
                    "description": "Obtener información de precios y políticas del consultorio"
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "find_next_available_slot",
                    "description": "Buscar próximos horarios disponibles para citas"
                }
            }
        ]
        
        # Llamada a OpenAI
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        
        # Verificar si debe ejecutar una herramienta
        if response.choices[0].message.tool_calls:
            tool_call = response.choices[0].message.tool_calls[0]
            function_name = tool_call.function.name
            
            # Ejecutar herramienta correspondiente
            try:
                if function_name == "read_sheet_data":
                    sheet_data = read_sheet_data()
                    return f"Información actualizada: {sheet_data.get('precio_consulta', 'No disponible')}"
                
                elif function_name == "find_next_available_slot":
                    slot = find_next_available_slot()
                    return f"Horario disponible: {slot['start_time']}" if slot else "No hay horarios"
            
            except ConnectionError as e:
                error_code = str(e)
                return format_error_response(error_code)
        
        # Si no usa herramientas, devolver respuesta normal
        ai_response = response.choices[0].message.content
        logger.info(f"🤖 OpenAI respondió en {time.time() - start_time:.2f}s")
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error en OpenAI: {str(e)}")
        return "Lo siento, estoy teniendo dificultades técnicas. ¿Podría repetir su pregunta?"








# ==================================================
# Parte 3: Manejo de errores estructurado
# ==================================================
# Propósito:
# - Traducir códigos de error técnicos a mensajes de usuario
# - Mantener consistencia en las respuestas

def format_error_response(error_code: str) -> str:
    """
    Convierte códigos de error técnicos en mensajes amigables
    """
    error_messages = {
        "GOOGLE_SHEETS_UNAVAILABLE": "No puedo acceder a la base de datos en este momento",
        "GOOGLE_CALENDAR_UNAVAILABLE": "El sistema de citas no está disponible ahora",
        "DEFAULT": "Estoy teniendo problemas técnicos"
    }
    return f"[ERROR] {error_messages.get(error_code, error_messages['DEFAULT'])}"







# ==================================================
# Parte 4: Punto de entrada para pruebas locales
# ==================================================
# Propósito:
# - Permitir ejecución directa para debugging
# - Ejemplo de uso básico

if __name__ == "__main__":
    # Simular conversación de prueba
    test_history = [
        {"role": "user", "content": "¿Cuánto cuesta una consulta?"}
    ]
    
    print("Testeando IA...")
    response = generate_openai_response(test_history)
    print(f"Respuesta: {response}")