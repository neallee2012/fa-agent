FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
# --no-deps + pip check: a dependency missing from the pinned closure fails the build instead of pulling an unpinned release.
RUN pip install --no-cache-dir --no-deps -r requirements.txt && pip check

COPY main.py .

EXPOSE 8088
CMD ["python", "main.py"]
