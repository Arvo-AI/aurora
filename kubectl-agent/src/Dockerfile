FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
ARG KUBECTL_VERSION=v1.31.0
ARG TARGETARCH=amd64
RUN curl -LO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" && chmod +x kubectl

FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/* && apt-get clean
RUN useradd -m -u 1000 -s /bin/bash kubectl-agent
COPY --from=builder --chown=kubectl-agent:kubectl-agent /root/.local /home/kubectl-agent/.local
COPY --from=builder /build/kubectl /usr/local/bin/kubectl
COPY --chown=kubectl-agent:kubectl-agent agent.py .
USER kubectl-agent
ENV PATH=/home/kubectl-agent/.local/bin:$PATH \
    PYTHONPATH=/home/kubectl-agent/.local/lib/python3.11/site-packages:$PYTHONPATH \
    PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import sys; sys.exit(0)" || exit 1
CMD ["python", "-u", "agent.py"]

