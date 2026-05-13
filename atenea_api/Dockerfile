FROM python:3.11-slim as compiler

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Set the working directory
WORKDIR /app/

# Install required system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv

# Activate the virtual environment and install dependencies
COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -Ur /app/requirements.txt

# Final runtime image
FROM python:3.11-slim as runner

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Set the working directory
WORKDIR /app/

# Copy the virtual environment from the compiler stage
COPY --from=compiler /opt/venv /opt/venv

# Install wget (required by download.sh)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy the application code
COPY . /app/

# Download data and models
RUN chmod +x ./download.sh
RUN ./download.sh
