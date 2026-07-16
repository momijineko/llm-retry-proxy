FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY retry_proxy ./retry_proxy
COPY stats.html ./
COPY logs.html ./

CMD ["python", "main.py"]
