# One image, four services. The bridge, provisioner and predictor in stages C
# and D use the same image with a different `command:`, so there is a single
# thing to build and a single version to reason about.
FROM python:3.12-slim

# pika       -> bootstrap, synchronous AMQP for declaring the topology
# aio-pika   -> bridge and predictor, asyncio AMQP
# autobahn   -> bridge and mock board, WAMP over TLS to Crossbar
# cbor2      -> WITHOUT THIS autobahn cannot build the CBOR serializer that
#               Crossbar offers, and every connect dies with
#               'could not create serializer for "cbor" (available: [json])'.
#               Same trap CLAUDE.md records for lightning-rod.
# pymysql    -> provisioner, reads the IoTronic boards table
# aiohttp    -> audit logger query API, SSE and WebSocket, and the
#               conductor proxy that captures operator identity
RUN pip install --no-cache-dir \
        pika==1.3.2 \
        aio-pika==9.5.7 \
        autobahn==24.4.2 \
        cbor2==5.6.5 \
        pymysql==1.1.1 \
        aiohttp==3.10.11 \
    && rm -rf /root/.cache

WORKDIR /opt/s4t
COPY scripts/s4t_ditto_bootstrap.py ./
COPY scripts/s4t_ditto_bridge.py ./
COPY scripts/s4t_ditto_provisioner.py ./
COPY scripts/s4t_mock_board.py ./
COPY scripts/s4t_audit_logger.py ./
COPY scripts/s4t_audit_proxy.py ./
COPY scripts/s4t_dashboard.py ./
# stage D adds:
# COPY scripts/s4t_predictor.py ./
RUN chmod +x *.py

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3"]