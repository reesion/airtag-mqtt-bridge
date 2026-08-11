FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY _login.py airtag_tracker.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# /app/data is where the named Docker volume gets mounted (config.yaml,
# records/, and all persistent state live there -- see docker-compose.yml).
RUN mkdir -p /app/data
WORKDIR /app/data

CMD ["/app/entrypoint.sh"]
