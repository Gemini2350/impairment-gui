FROM python:3.12-alpine

RUN pip install --no-cache-dir flask

WORKDIR /app
COPY app.py .

ENV DATA_DIR=/data
RUN mkdir -p /data
VOLUME /data

EXPOSE 8080
CMD ["python", "app.py"]
