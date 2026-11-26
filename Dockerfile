# The delivery photos app (UI + photo logic) in one image, served by gunicorn on port 8080.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

# Shown in the page footer so a release is visible; the workflow passes the commit SHA.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

RUN useradd --system --no-create-home onward
USER onward

EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "app:app"]
