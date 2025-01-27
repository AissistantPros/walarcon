from decouple import config
import openai

# Configuración de la API Key de OpenAI
CHATGPT_SECRET_KEY = config("CHATGPT_SECRET_KEY")

# Inicializar el cliente de OpenAI
openai.api_key = CHATGPT_SECRET_KEY

def generate_openai_response(prompt, max_tokens=400, temperature=0.7, model="gpt-4o"):
    """
    Genera una respuesta usando OpenAI a partir de un prompt.

    Args:
        prompt (str): Texto de entrada para generar la respuesta.
        max_tokens (int): Número máximo de tokens que puede contener la respuesta.
        temperature (float): Nivel de aleatoriedad de la respuesta (0.0 a 1.0).
        model (str): Modelo de OpenAI que se utilizará.

    Returns:
        str: Respuesta generada por OpenAI o un mensaje de error.
    """
    try:
        response = openai.Completion.create(
            model=model,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response["choices"][0]["text"].strip()
    except Exception as e:
        print(f"Error generando respuesta con OpenAI: {e}")
        return "Lo siento, hubo un error procesando tu solicitud."
