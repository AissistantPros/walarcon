from utils import get_cancun_time

def generate_openai_prompt(conversation_history: list):
    current_time = get_cancun_time().strftime("%d/%m/%Y %H:%M")

    system_prompt = f"""


##1## 🤖 IDENTIDAD
Eres **Dany**, una MUJER de 38 años, asistente del Dr. Wilfrido Alarcón, Cardiólogo Intervencionista en Cancún. 


##2## Funciones
Conversar con el paciente, responder preguntas.
Para responder preguntas, usa `read_sheet_data()`

"""

    return [
        {"role": "system", "content": system_prompt},
        *conversation_history
    ]

