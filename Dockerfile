FROM python:3.11.14-slim-bookworm
WORKDIR /workspace
COPY requirements.lock .
RUN python3 -m pip install --no-cache-dir -r requirements.lock
COPY . .
CMD ["python3", "-m", "app", "demo"]
