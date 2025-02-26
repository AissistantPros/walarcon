FROM python:3.9-slim

# 1. Instalar FFmpeg y dependencias críticas
RUN apt-get update && apt-get install -y ffmpeg build-essential

# 2. Copiar SOLO requirements.txt primero (optimización clave)
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 3. Copiar el resto del proyecto
COPY . .

# 4. Comando para iniciar la app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]