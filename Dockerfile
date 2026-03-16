# Imagem base Python (slim = menor tamanho)
FROM python:3.11-slim

# Diretório de trabalho no container
WORKDIR /app

# Copia e instala dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY main.py api_call.py batch_weather.py municipios_reader.py .
COPY municipios.csv .

# Cloud Run expõe a porta via variável PORT (padrão 8080)
ENV PORT=8080
EXPOSE 8080

# Gunicorn para servir o app Flask; escuta no host e porta corretos
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} main:app"]
