FROM python:3.9-slim

# 1. Instalar FFmpeg y dependencias del sistema
RUN apt-get update && apt-get install -y ffmpeg

# 2. Copiar SOLO requirements.txt primero (para cachear dependencias)
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 3. Copiar el resto del proyecto
COPY . .

# 4. Comando para iniciar la app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]