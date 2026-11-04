"""python -m iassistpro: serve the interviewer on 0.0.0.0:$PORT (access log off: it would print ?token=)."""

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    s = Settings()
    uvicorn.run(create_app(s), host="0.0.0.0", port=s.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
