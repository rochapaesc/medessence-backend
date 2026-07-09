FROM python:3.13-slim

ARG REQUIREMENTS_PATH=requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/home/app

RUN mkdir $APP_HOME
WORKDIR $APP_HOME

RUN apt-get update && apt-get install -y netcat-openbsd && rm -rf /var/lib/apt/lists/*

COPY ${REQUIREMENTS_PATH} $APP_HOME/${REQUIREMENTS_PATH}
RUN pip install --upgrade --no-cache pip
RUN pip install --no-cache-dir -r $APP_HOME/$REQUIREMENTS_PATH

COPY . $APP_HOME

COPY ./docker/celery/entrypoint.sh .
RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["/home/app/docker/celery/entrypoint.sh"]
