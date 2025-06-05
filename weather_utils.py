# weather_utils.py
# -*- coding: utf-8 -*-
"""
Módulo para obtener información del clima utilizando OpenWeatherMap.
"""

import logging
import requests # Asegúrate que 'requests' esté en tu requirements.txt
from decouple import config
from datetime import datetime

logger = logging.getLogger(__name__)

# Intenta obtener la API key desde las variables de entorno.
# Si no está definida, OPENWEATHERMAP_API_KEY será None.
OPENWEATHERMAP_API_KEY = config("OPENWEATHERMAP_API_KEY", default=None)

# ID de la ciudad de Cancún en OpenWeatherMap. Puedes encontrar otros IDs en su sitio.
CANCUN_CITY_ID = "3530103"

def get_cancun_weather() -> dict:
    """
    Obtiene el clima actual para Cancún desde OpenWeatherMap.

    Retorna:
        dict: Un diccionario con la información del clima o un mensaje de error.
              Ejemplo de éxito:
              {
                  "cancun_weather": {
                      "current": {
                          "description": "Cielo claro",
                          "temperature": "28°C",
                          "feels_like": "30°C",
                          "humidity": "75%",
                          "wind_speed": "3.5 m/s",
                          "icon_code": "01d"
                      }
                  }
              }
              Ejemplo de error:
              {
                  "error": "Mensaje describiendo el problema."
              }
    """
    if not OPENWEATHERMAP_API_KEY:
        logger.error("OPENWEATHERMAP_API_KEY no está configurada en las variables de entorno. No se puede obtener el clima.")
        return {"error": "Servicio de clima no disponible (API key no configurada)."}

    weather_data_payload = {} # Lo que se devolverá a la IA

    try:
        # URL para el clima actual.
        # units=metric para grados Celsius.
        # lang=es para descripciones en español.
        url_current = f"http://api.openweathermap.org/data/2.5/weather?id={CANCUN_CITY_ID}&appid={OPENWEATHERMAP_API_KEY}&units=metric&lang=es"

        logger.info(f"Solicitando clima actual a OpenWeatherMap para Cancún (ID: {CANCUN_CITY_ID})...")
        response_current = requests.get(url_current, timeout=10) # Timeout de 10 segundos
        response_current.raise_for_status() # Esto lanzará una excepción para errores HTTP (4xx o 5xx)
        
        data_current = response_current.json()
        logger.debug(f"Respuesta de OpenWeatherMap (clima actual): {data_current}")

        # Extraer la información relevante
        description = data_current.get("weather", [{}])[0].get("description", "No disponible").capitalize()
        temperature = data_current.get("main", {}).get("temp", "N/A")
        feels_like = data_current.get("main", {}).get("feels_like", "N/A")
        humidity = data_current.get("main", {}).get("humidity", "N/A")
        wind_speed = data_current.get("wind", {}).get("speed", "N/A")
        icon_code = data_current.get("weather", [{}])[0].get("icon", None)

        weather_data_payload["current"] = {
            "description": description,
            "temperature": f"{temperature}°C" if isinstance(temperature, (int, float)) else str(temperature),
            "feels_like": f"{feels_like}°C" if isinstance(feels_like, (int, float)) else str(feels_like),
            "humidity": f"{humidity}%" if isinstance(humidity, (int, float)) else str(humidity),
            "wind_speed": f"{wind_speed} m/s" if isinstance(wind_speed, (int, float)) else str(wind_speed),
            "icon_code": icon_code
        }
        
        return {"cancun_weather": weather_data_payload}

    except requests.exceptions.Timeout:
        logger.error("Timeout al intentar conectar con OpenWeatherMap.")
        return {"error": "No se pudo contactar el servicio de clima (timeout). Intente más tarde."}
    except requests.exceptions.HTTPError as http_err:
        logger.error(f"Error HTTP de OpenWeatherMap: {http_err}. Respuesta: {http_err.response.text}")
        if http_err.response.status_code == 401:
            return {"error": "Error de autenticación con el servicio de clima (API key inválida o problema de suscripción)."}
        elif http_err.response.status_code == 404:
            return {"error": "No se encontró la ciudad para el clima (configuración incorrecta)."}
        elif http_err.response.status_code == 429:
            return {"error": "Se ha excedido el límite de solicitudes al servicio de clima. Intente más tarde."}
        return {"error": f"No se pudo obtener el clima debido a un error del servidor ({http_err.response.status_code})."}
    except requests.exceptions.RequestException as req_err:
        logger.error(f"Error de conexión general al obtener clima de OpenWeatherMap: {req_err}")
        return {"error": "No se pudo conectar con el servicio de clima. Verifique su conexión a internet."}
    except Exception as e_general:
        # Captura cualquier otro error inesperado durante el procesamiento.
        logger.error(f"Error inesperado al procesar datos del clima: {e_general}", exc_info=True)
        return {"error": "Ocurrió un error inesperado al procesar la información del clima."}

