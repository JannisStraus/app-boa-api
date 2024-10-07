# Use Python 3.10 slim as the base image
FROM python:3.10-slim

# Set default versions for Pixman and Cairo
ARG PIXMAN_VERSION=0.38.4
ARG CAIRO_VERSION=1.16.0

# Install necessary build dependencies
RUN apt update && apt install -y \
    build-essential \
    curl \
    libffi-dev \
    libgdk-pixbuf2.0-dev \
    libpango1.0-dev \
    libcairo2-dev \
    libjpeg-dev \
    libpng-dev \
    libz-dev \
    libgl1 \
    dcmtk \
    xz-utils \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Build Pixman and Cairo from source
RUN curl -sSL https://cairographics.org/releases/pixman-${PIXMAN_VERSION}.tar.gz | \
    tar xvz -C /tmp && \
    cd /tmp/pixman-${PIXMAN_VERSION} && \
    ./configure --prefix=/opt/pixman && \
    make -j"$(nproc)" && \
    make install && \
    curl -sSL https://cairographics.org/releases/cairo-${CAIRO_VERSION}.tar.xz | \
    tar xvJ -C /tmp && \
    cd /tmp/cairo-${CAIRO_VERSION} && \
    ./configure --prefix=/opt/cairo && \
    make -j"$(nproc)" && \
    make install && \
    cd / && \
    rm -rf /tmp/pixman-${PIXMAN_VERSION} /tmp/cairo-${CAIRO_VERSION}

# Set environment for Cairo and Pixman
ENV LD_LIBRARY_PATH=/opt/cairo/lib:/opt/pixman/lib:$LD_LIBRARY_PATH

# Set the working directory
WORKDIR /src

# Copy the application source code
COPY src /src

# Install Poetry and dependencies
ARG GITLAB_PYPI_USERNAME
ARG GITLAB_PYPI_PASSWORD
ARG POETRY_VERSION=1.5.1  # Update to a more recent Poetry version

# Copy the pyproject.toml and poetry.lock files
COPY pyproject.toml poetry.lock /src/
# COPY body-organ-analysis /src/

# Install Poetry and dependencies
RUN python -m pip install --upgrade pip && \
    pip install "poetry==$POETRY_VERSION" && \
    poetry config virtualenvs.create false && \
    POETRY_HTTP_BASIC_SHIPAISEGCLI_USERNAME=$GITLAB_PYPI_USERNAME \
    POETRY_HTTP_BASIC_SHIPAISEGCLI_PASSWORD=$GITLAB_PYPI_PASSWORD \
    poetry install --no-root && \
    rm pyproject.toml poetry.lock

RUN rm -r /usr/local/lib/python3.10/site-packages/body_organ_analysis/_external
COPY /_external /usr/local/lib/python3.10/site-packages/body_organ_analysis/_external

# Expose necessary ports (if needed)
# EXPOSE 8000

# Set the entrypoint or command if not set in docker-compose.yaml
# CMD ["uvicorn", "boa_ai.api:app", "--reload", "--host", "0.0.0.0"]
