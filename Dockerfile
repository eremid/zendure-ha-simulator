FROM python:3.12-slim

WORKDIR /app

# Only paho-mqtt is needed at runtime.
# pandas/matplotlib (in requirements.txt) are only for plot.py.
RUN pip install --no-cache-dir "paho-mqtt>=2.0.0"

COPY simulate.py .

# ZenSDK HTTP server port (matches HTTP_PORT env var default)
EXPOSE 8088

# -u = unbuffered stdout so logs appear immediately in 'docker compose logs'
CMD ["python", "-u", "simulate.py"]
