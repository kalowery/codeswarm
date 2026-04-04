FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex@latest

RUN python3 -m pip install --no-cache-dir --break-system-packages claude-agent-sdk

WORKDIR /workspace
