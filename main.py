import sys

try:
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    getattr(sys.stderr, "reconfigure", lambda **_: None)(encoding="utf-8")
except Exception:
    pass

from retry_proxy.application import app, settings


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.listen_host, port=settings.listen_port, log_config=None, access_log=False)
