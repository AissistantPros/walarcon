# -*- coding: utf-8 -*-
"""
Módulo para leer datos de Google Sheets.
Utilizado para obtener información como precios, políticas y otros datos del consultorio.
"""

# ==================================================
# 🔹 Parte 1: Importaciones y Configuración
# ==================================================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables de configuración de Google Sheets
GOOGLE_SHEETS_ID = config("GOOGLE_SHEETS_ID")  # ID del archivo de Google Sheets
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")  # Llave privada con formato correcto
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")  # ID del proyecto
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")  # Correo de la cuenta de servicio






# ==================================================
# 🔹 Parte 2: Inicialización de Google Sheets
# ==================================================
def initialize_google_sheets():
    """
    Configura y conecta la API de Google Sheets usando credenciales de servicio.

    Retorna:
        object: Cliente autenticado de Google Sheets.
    """
    try:
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
    except Exception as e:
        logger.error(f"Error al conectar con Google Sheets: {str(e)}")
        raise ConnectionError("GOOGLE_SHEETS_UNAVAILABLE")  # Nueva línea para manejo de error








# ==================================================
# 🔹 Parte 3: Función para leer datos de Google Sheets
# ==================================================
def read_sheet_data(sheet_range="Generales!A:B"):
    """
    Lee datos de Google Sheets en un rango específico.

    Parámetros:
        sheet_range (str): Rango a leer (por defecto: claves en columna A, valores en B).

    Retorna:
        dict: Diccionario con claves y valores obtenidos de Google Sheets.
    """
    try:
        service = initialize_google_sheets()
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=GOOGLE_SHEETS_ID, range=sheet_range).execute()
        rows = result.get("values", [])

        if not rows:
            logger.warning("⚠️ La hoja está vacía o no se encontraron datos.")
            return {}

        return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}

    except Exception as e:
        logger.error(f"Error al leer datos de Google Sheets: {str(e)}")
        raise ConnectionError("GOOGLE_SHEETS_UNAVAILABLE")  # Nueva línea para manejo de error









# ==================================================
# 🔹 Parte 4: Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar la conexión y lectura de datos en Google Sheets.
    """
    try:
        data = read_sheet_data()
        if data:
            print("✅ Datos obtenidos de Google Sheets:")
            for key, value in data.items():
                print(f"{key}: {value}")
        else:
            print("⚠️ No se encontraron datos en la hoja de cálculo.")

    except ConnectionError as ce:
        print(f"❌ Error de conexión con Google Sheets: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido al leer datos: {str(e)}")
