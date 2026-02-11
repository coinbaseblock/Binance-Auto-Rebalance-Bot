# syntax=docker/dockerfile:1
# Use full Python image instead of slim to avoid OCI runtime issues on Windows Docker Desktop
FROM --platform=linux/amd64 python:3.12

WORKDIR /app

# Environment variables to reduce resource usage and avoid container creation issues
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_WARN_SCRIPT_LOCATION=1

# Copy requirements first
COPY requirements.txt /app/

# Install dependencies in separate small batches to avoid OCI runtime container creation failures
# This reduces peak memory/resource usage during each container operation
RUN pip install --no-compile python-binance==1.0.19 python-dotenv==1.0.0 pyyaml==6.0.1 requests==2.31.0 tabulate==0.9.0
RUN pip install --no-compile numpy==1.26.2
RUN pip install --no-compile pandas==2.1.4
RUN pip install --no-compile matplotlib==3.8.2
RUN pip install --no-compile ta==0.11.0
RUN pip install --no-compile ccxt==4.2.25
RUN pip install --no-compile flask==3.0.0 flask-socketio==5.3.6 python-socketio==5.10.0 eventlet==0.34.2
RUN pip install --no-compile pytest==7.4.3

# Copy application code
COPY . /app/

# Create directories
RUN mkdir -p /app/logs /app/data/historical /app/charts_output

EXPOSE 5000

# Default command runs dashboard mode
CMD ["python", "main.py", "--mode", "dashboard", "--port", "5000"]
