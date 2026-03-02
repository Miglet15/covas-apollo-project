FROM python:3.11-slim

WORKDIR /app

# Install sqlite3 CLI + dependencies
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy service files
COPY main.py summarizer.py storage.py models.py ./

# Data volume mount point
RUN mkdir -p /data

EXPOSE 8100

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8100"]
