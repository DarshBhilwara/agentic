FROM vllm/vllm-openai:v0.8.5
COPY inference /opt/agentic-telemetry
RUN pip install --no-cache-dir /opt/agentic-telemetry opentelemetry-sdk==1.26.0 opentelemetry-exporter-otlp==1.26.0
ENV VLLM_USE_V1=0
