FROM python:3.14-slim

WORKDIR /app

# dependências primeiro (camada cacheada — só reinstala se requirements mudar)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# código por último
COPY . .

CMD ["python", "-m", "src.main"]