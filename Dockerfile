FROM python:3.12-slim
WORKDIR /app
# ffmpeg converts the printer's RTSPS camera stream to JPEG
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY bambu_errors_data.json .
COPY templates templates
COPY static static
ENV DATA_DIR=/data
EXPOSE 5000
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]
