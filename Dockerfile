FROM python:3.11-slim
 
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl && \
    rm -rf /var/lib/apt/lists/*
 
WORKDIR /app
 
COPY Requirements.txt .
RUN pip install --no-cache-dir -r Requirements.txt
 
COPY app.py .
COPY newsbot.py .
COPY config.yaml .
COPY static/ ./static/
 
 
EXPOSE 8000
 
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]