"""OpenTelemetry setup — mirrors pattern used by fitness-machine-mcp and livetrack-mcp."""
from __future__ import annotations
import logging
import os

log = logging.getLogger("intervals_gateway.otel")


def setup_otel() -> None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint:
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — skipping telemetry")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource

        service_name = os.environ.get("OTEL_SERVICE_NAME", "intervals-gateway")
        resource_attrs = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")

        attrs = {"service.name": service_name}
        for kv in resource_attrs.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                attrs[k.strip()] = v.strip()

        provider = TracerProvider(resource=Resource.create(attrs))
        otlp_endpoint = endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
        trace.set_tracer_provider(provider)

        log.info("OTEL tracing enabled → %s (service=%s)", endpoint, service_name)
    except Exception as e:
        log.warning("OTEL setup failed (non-fatal): %s", e)
