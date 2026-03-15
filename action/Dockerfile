FROM python:3.13-slim

WORKDIR /app

COPY action/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY action/entrypoint.py .

ENTRYPOINT ["python", "entrypoint.py"]
