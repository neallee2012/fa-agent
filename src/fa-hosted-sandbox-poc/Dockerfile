FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        cifs-utils \
        curl \
        imagemagick \
        iproute2 \
        netcat-openbsd \
        openbox \
        passwd \
        procps \
        util-linux \
        xdotool \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin pocbrowser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN PYTHONDONTWRITEBYTECODE= python -m compileall -q .

EXPOSE 8088

CMD ["python", "main.py"]
