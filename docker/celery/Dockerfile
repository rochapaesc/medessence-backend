FROM python:3.13-slim

ARG REQUIREMENTS_PATH=requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/home/app

RUN mkdir $APP_HOME
WORKDIR $APP_HOME

# ffmpeg: é o worker que baixa a mídia do WhatsApp e lê duração e desenho de
# onda do áudio (apps/inbox/audio.py). Sem ele o download continua funcionando
# e a tela cai na barra simples — mas é AQUI que ele faz falta.
RUN apt-get update \
    && apt-get install -y --no-install-recommends netcat-openbsd ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY ${REQUIREMENTS_PATH} $APP_HOME/${REQUIREMENTS_PATH}
RUN pip install --upgrade --no-cache pip
RUN pip install --no-cache-dir -r $APP_HOME/$REQUIREMENTS_PATH

COPY . $APP_HOME

COPY ./docker/celery/entrypoint.sh .
RUN chmod +x ./entrypoint.sh

ENTRYPOINT ["/home/app/docker/celery/entrypoint.sh"]
