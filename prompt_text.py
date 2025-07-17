# prompt_text.py
from utils import get_cancun_time
from typing import List, Dict

def generate_openai_prompt(conversation_history: List[Dict]) -> List[Dict]:
    """
    Genera el prompt para OpenAI basado en el historial de conversación.
    
    Args:
        conversation_history: Lista de mensajes de la conversación
        
    Returns:
        Lista de diccionarios con el prompt formateado para OpenAI
    """
    # Implementación básica del prompt
    system_prompt = {
        "role": "system",
        "content": """Eres un asistente virtual amigable y profesional para una clínica médica. 
        Tu función es ayudar a los pacientes a agendar, modificar y cancelar citas médicas.
        
        REGLAS IMPORTANTES:
        - Siempre usa números en formato numérico (no palabras como "quince", "veintiuno")
        - Para teléfonos, usa exactamente 10 dígitos sin espacios, puntos, comas ni guiones
        - Si el usuario dicta un número incorrecto, pídele que lo repita antes de usar herramientas
        - Sé amigable, profesional y paciente con los usuarios
        - Si no entiendes algo, pide aclaración amablemente"""
    }
    
    # Convertir el historial de conversación al formato requerido
    messages = [system_prompt]
    
    for message in conversation_history:
        if message.get("role") and message.get("content"):
            messages.append({
                "role": message["role"],
                "content": message["content"]
            })
    
    return messages