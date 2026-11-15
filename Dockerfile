# Один образ и для балансировщика, и для тестовых бэкендов:
# у нас нет внешних зависимостей, поэтому образ получается очень маленьким.
FROM python:3.12-slim

# openssl нужен только скрипту gen-cert.sh (сервис certgen в docker-compose)
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl \
 && rm -rf /var/lib/apt/lists/*

# Не запускаем процессы от root: если в коде найдут дыру, у атакующего будет меньше прав.
RUN useradd --system --uid 10001 --no-create-home app

# Каталог для файла состояния admin API. Создаём его здесь и отдаём пользователю app:
# том, смонтированный в /state, унаследует эти права, и балансировщик сможет в него писать.
RUN mkdir /state && chown app /state

WORKDIR /app
COPY mylb ./mylb
COPY demo_backend.py ./
COPY scripts ./scripts

# PYTHONUNBUFFERED: логи сразу попадают в `docker compose logs`, а не застревают в буфере.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER app

# По умолчанию запускается балансировщик; бэкенды переопределяют entrypoint.
ENTRYPOINT ["python", "-m", "mylb"]
