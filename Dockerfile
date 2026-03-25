FROM python:3.12-slim

WORKDIR /app

COPY monitor.py README.md ./
COPY env ./env
COPY start.sh ./start.sh

RUN chmod +x /app/start.sh

CMD ["./start.sh"]
