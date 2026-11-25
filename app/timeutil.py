from datetime import timezone
from zoneinfo import ZoneInfo
from config import Config


def to_local(dt, fmt='%d/%m %H:%M'):
    """Convierte un datetime UTC 'naive' de la BD a la zona horaria configurada (APP_TIMEZONE)."""
    if not dt:
        return ''
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(Config.APP_TIMEZONE)).strftime(fmt)
