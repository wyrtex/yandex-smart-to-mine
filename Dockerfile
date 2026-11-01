FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 SMARTHOME_DATA_DIR=/data
COPY pyproject.toml README.md ./
COPY smarthome ./smarthome
RUN pip install --no-cache-dir .
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "smarthome", "serve"]
