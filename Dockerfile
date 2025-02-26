FROM python:3.9-slim

# 1. Instalar FFmpeg y dependencias
RUN apt-get update && apt-get install -y ffmpeg

# 2. Copiar todo el proyecto
WORKDIR /app
COPY . .

# 3. Instalar dependencias con pip
RUN pip install --upgrade pip && pip install -r requirements.txt

# 4. Comando para iniciar la app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]