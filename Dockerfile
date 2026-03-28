FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py README.md ./

# If you use start.sh or env/, add:
# COPY start.sh ./
# COPY env ./env
# RUN chmod +x /app/start.sh

CMD ["python", "-u", "monitor.py"]
