# Use Python 3.12 slim image for smaller size
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire project
COPY . .

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Create necessary directories
RUN mkdir -p data/raw output/pinit_demo

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src

# Expose port (Cloud Run will set PORT=8080)
EXPOSE 8080

# Use entrypoint script
ENTRYPOINT ["/app/entrypoint.sh"]
