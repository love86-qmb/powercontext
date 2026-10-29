# seekdb 记忆桥：只用标准库 HTTP 服务 + pyseekdb，镜像很小
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEEKDB_HOST=seekdb \
    SEEKDB_PORT=2881 \
    SEEKDB_USER=root \
    SEEKDB_DB=test

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY embedding.py app.py ./

EXPOSE 8077

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=10 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8077/health',timeout=4)"

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "8077"]
