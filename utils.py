from datetime import datetime
import pytz

def get_cancun_time():
    """Obtiene la fecha y hora actual en la zona horaria de Cancún."""
    cancun_tz = pytz.timezone("America/Cancun")
    now = datetime.now(cancun_tz)
    return now

def get_iso_format():
    """Convierte la fecha y hora de Cancún al formato ISO 8601."""
    now = get_cancun_time()
    return now.isoformat()