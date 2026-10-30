FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
LABEL trellis.worker.protocol="public-document-v1" \
    trellis.browser.protocol="public-render-v1" \
    trellis.browser.playwright-version="1.64.1" \
    trellis.browser.seccomp-sha256="9c86a46bf3b2ef2c7cfece632b4ece1127698810857ef98914f87d99fdc74a27"
WORKDIR /opt/trellis
COPY containers/worker-requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm /tmp/requirements.txt \
    && rm -rf /var/lib/apt/lists/*
COPY app/domain /opt/trellis/app/domain
COPY app/infrastructure/web_reader.py app/infrastructure/web_network.py app/infrastructure/web_extraction.py app/infrastructure/pdf_extractor.py app/infrastructure/browser_http.py app/infrastructure/web_renderer.py /opt/trellis/app/infrastructure/
COPY containers/chromium-seccomp.json containers/chromium-seccomp.LICENSE containers/chromium-seccomp.README.md /opt/trellis/browser-policy/
RUN touch /opt/trellis/app/__init__.py /opt/trellis/app/infrastructure/__init__.py
ENV HOME=/tmp XDG_CONFIG_HOME=/tmp/trellis-config XDG_CACHE_HOME=/tmp/trellis-cache
USER 10001:10001
CMD ["python", "-m", "app.infrastructure.web_reader"]
