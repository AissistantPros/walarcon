from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config

# **SECCIÓN 1: Configuración de Google Sheets**
GOOGLE_SHEETS_ID = config("GOOGLE_SHEETS_ID")  # ID del archivo de Google Sheets
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")  # Llave privada con formato correcto
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")  # ID del proyecto
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")  # Correo de la cuenta de servicio

def initialize_google_sheets():
    """
    Configura y conecta la API de Google Sheets usando credenciales de servicio.

    Retorna:
        object: Cliente de Google Sheets.
    """
    credentials = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=credentials)

# **SECCIÓN 2: Función para leer datos de Google Sheets**
def read_sheet_data(sheet_range="Generales!A:B"):
    """
    Lee datos de Google Sheets en un rango específico.

    Parámetros:
        sheet_range (str): Rango a leer (por defecto: claves en columna A, valores en B).

    Retorna:
        dict: Diccionario con claves y valores obtenidos de Google Sheets.
    """
    service = initialize_google_sheets()
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=GOOGLE_SHEETS_ID, range=sheet_range).execute()
    rows = result.get("values", [])
    return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}

# **SECCIÓN 3: Ejecución para pruebas locales**
if __name__ == "__main__":
    try:
        # Probar la lectura de datos
        data = read_sheet_data()
        print("Datos obtenidos de Google Sheets:")
        for key, value in data.items():
            print(f"{key}: {value}")
    except Exception as e:
        print("Error al leer datos de Google Sheets:", str(e))
