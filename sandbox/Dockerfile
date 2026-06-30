FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        cargo \
        curl \
        default-jdk-headless \
        git \
        golang-go \
        jq \
        nodejs \
        npm \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
CMD ["/bin/sh"]
