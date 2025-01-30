from openai import OpenAI
from decouple import config

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

def generate_openai_response(prompt, model="gpt-4o", max_tokens=400, temperature=0.7):
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature
        )
        return response.choices[0].message.content.strip()  # Acceso correcto
    except Exception as e:
        print(f"Error en OpenAI: {e}")
        return "Lo siento, hubo un error procesando tu solicitud."