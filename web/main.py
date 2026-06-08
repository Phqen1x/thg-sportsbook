from __future__ import annotations

import uvicorn

from web import config
from web.app import app

if __name__ == "__main__":
    uvicorn.run(
        "web.app:app",
        host=config.WEB_HOST,
        port=config.WEB_PORT,
        reload=False,
    )
