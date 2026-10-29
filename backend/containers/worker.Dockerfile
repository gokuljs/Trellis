FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/trellis
COPY containers/worker-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY app/domain /opt/trellis/app/domain
COPY app/infrastructure/web_reader.py app/infrastructure/web_network.py /opt/trellis/app/infrastructure/
RUN touch /opt/trellis/app/__init__.py /opt/trellis/app/infrastructure/__init__.py
USER 10001:10001
CMD ["python", "-m", "app.infrastructure.web_reader"]
