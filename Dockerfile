# ============================================================
# Chatbot WhatsApp Business (Flask + Gunicorn) — python:3.12-slim
# La imagen NO contiene secretos: todo llega por variables de entorno (.env).
# ============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh \
 && useradd --system --uid 10001 --no-create-home appuser \
 && mkdir -p /app/data/comprobantes \
 && chown -R appuser /app
USER appuser

EXPOSE 5005

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5005/health', timeout=5).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
