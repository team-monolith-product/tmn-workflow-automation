FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip git curl jq ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt ./
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY bridge.py entrypoint.sh ./
COPY harness/ ./harness/
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
