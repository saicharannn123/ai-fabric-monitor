FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fabric_exporter ./fabric_exporter
EXPOSE 9342
ENTRYPOINT ["python", "-m", "fabric_exporter"]
