from __future__ import annotations

import uvicorn

from web import config
from web.app import app


def main() -> None:
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
