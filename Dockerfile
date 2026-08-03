# Imagen oficial de Playwright: ya trae Chromium y todas las dependencias del sistema
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY td_shot.py .

CMD ["python", "td_shot.py"]
