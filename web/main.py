from __future__ import annotations

import uvicorn

from web import config
from web.app import app


def main() -> None:
    if config.WEB_SECRET_KEY == "dev-secret-change-me":
        raise RuntimeError(
            "WEB_SECRET_KEY is set to the insecure default. "
            "Set a strong random value in your .env before running in production."
        )
    uvicorn.run(
        "web.app:app",
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        reload=False,
        ssl_certfile=config.WEB_SSL_CERTFILE,
        ssl_keyfile=config.WEB_SSL_KEYFILE,
    )


if __name__ == "__main__":
    main()
