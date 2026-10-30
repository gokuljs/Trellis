"""Check the configured local web boundary without paid provider requests."""

import asyncio
import json
import logging

from app.core.config import Settings
from app.infrastructure.retrieval_worker import (
    DockerPublicRetriever,
    GatewayControlClient,
    WorkerDeployment,
)


async def check() -> dict[str, bool]:
    settings = Settings()
    if (
        settings.web_worker_image is None
        or settings.web_gateway_image is None
        or settings.web_gateway_secret_file is None
        or settings.web_gateway_admin_secret is None
    ):
        raise RuntimeError("Configure the web worker, gateway, and private control credential.")
    retriever = DockerPublicRetriever(
        WorkerDeployment(
            settings.web_worker_image,
            settings.web_gateway_image,
            settings.web_gateway_container,
            settings.web_gateway_ipc_volume,
            settings.web_gateway_secret_file,
        ),
        GatewayControlClient(
            settings.web_gateway_control_url,
            settings.web_gateway_admin_secret.get_secret_value().strip(),
        ),
    )
    return {
        "http": await retriever.is_ready(),
        "crawl": await retriever.is_crawl_ready(),
        "render": await retriever.is_render_ready(),
    }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = asyncio.run(check())
    print(json.dumps(result))
    if not all(result.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
