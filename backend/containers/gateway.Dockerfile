FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends iptables util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/trellis
COPY containers/gateway-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app/domain /opt/trellis/app/domain
COPY app/infrastructure/web_gateway.py app/infrastructure/web_network.py /opt/trellis/app/infrastructure/
COPY containers/gateway_guard.py /usr/local/lib/trellis/gateway_guard.py
COPY --chmod=755 containers/trellis-gateway-init containers/trellis-check-egress-policy /usr/local/bin/
RUN touch /opt/trellis/app/__init__.py /opt/trellis/app/infrastructure/__init__.py
CMD ["/usr/local/bin/trellis-gateway-init"]
