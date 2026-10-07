ARG DOCKER_REGISTRY=docker.io
ARG PYTHON_BASE_IMAGE=library/python:3.12-slim
FROM ${DOCKER_REGISTRY}/${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

COPY requirements.txt ./
RUN pip install --no-cache-dir --progress-bar off --index-url "$PIP_INDEX_URL" -r requirements.txt

COPY main.py ./
COPY retry_proxy ./retry_proxy
COPY stats.html ./
COPY logs.html ./
COPY requests.html ./
COPY key_pool.html ./
COPY settings.html ./

CMD ["python", "main.py"]
