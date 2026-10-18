FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-bpfcc python3-pip ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --break-system-packages --no-cache-dir 'redis>=5,<6'
WORKDIR /app
COPY kernel/collector.py /app/collector.py
COPY kernel/healthcheck.py /app/healthcheck.py
CMD ["python3", "/app/collector.py"]
