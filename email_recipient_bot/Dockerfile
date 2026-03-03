FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY email_recipient_bot/requirements.txt /app/email_recipient_bot/requirements.txt
RUN pip install --no-cache-dir -r /app/email_recipient_bot/requirements.txt

COPY email_recipient_bot /app/email_recipient_bot

EXPOSE 8090

CMD ["uvicorn", "email_recipient_bot.app:app", "--host", "0.0.0.0", "--port", "8090"]
