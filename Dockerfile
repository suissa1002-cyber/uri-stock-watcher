FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies first (better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Render injects PORT; expose 8000 as a default for local docker
EXPOSE 8000

# Persistent disk mount point — main.py / db.py expect /data/stock_watcher.db
# (only used if DATABASE_URL is not set — i.e. SQLite mode)
RUN mkdir -p /data

CMD ["python3", "main.py"]
