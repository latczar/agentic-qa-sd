FROM python:3.12-slim

WORKDIR /app

COPY telegram_bot/requirements.txt telegram_bot/requirements.txt
RUN pip install --no-cache-dir -r telegram_bot/requirements.txt

COPY shared/ shared/
COPY telegram_bot/ telegram_bot/

ENV PYTHONPATH=/app

CMD ["python", "-m", "telegram_bot.app.bot"]
