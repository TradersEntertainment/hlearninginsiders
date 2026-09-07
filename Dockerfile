FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1

# ROLE=census-worker → DB'siz sayım worker'ı (ana uygulamadan adres kiralar, HL'ye
# kendi IP'siyle sorar, sonucu geri yollar; /health'i kendi verir). Aksi hâlde ana uygulama.
CMD ["sh", "-c", "if [ \"$ROLE\" = \"census-worker\" ]; then python -m app.worker; else uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}; fi"]
