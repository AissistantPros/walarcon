FROM python:3.9-slim

# 1. Instalar FFmpeg y dependencias críticas
RUN apt-get update && apt-get install -y ffmpeg build-essential

# 2. Configurar entorno de trabajo
WORKDIR /app

# 3. Copiar SOLO requirements.txt primero (optimización clave)
COPY requirements.txt .

# 4. Instalar dependencias
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# 5. Copiar el resto del proyecto
COPY . .

# 6. Asegurar que Uvicorn está disponible
RUN pip install uvicorn

# 7. Comando para iniciar la app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$PORT"]
