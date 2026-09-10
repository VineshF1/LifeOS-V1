"""Dev-only launcher: this network's DNS refuses *.neon.tech, so resolve the
DB host via Google DNS-over-HTTPS and patch getaddrinfo before running.

Usage:
    .venv/Scripts/python.exe local_run.py initdb   # apply schema once
    .venv/Scripts/python.exe local_run.py serve    # start uvicorn on 8001

Not used in production. Permanent fix: set Wi-Fi DNS to 8.8.8.8 (needs admin).
"""
import json
import socket
import sys
import urllib.parse
import urllib.request

_REAL_GETADDRINFO = socket.getaddrinfo


def _doh_ipv4(host: str) -> list[str]:
    with urllib.request.urlopen(
        f"https://8.8.8.8/resolve?name={host}&type=A", timeout=10
    ) as r:
        data = json.load(r)
    return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]


def _db_host() -> str:
    sys.path.insert(0, ".")
    from app.config import settings

    url = settings.DATABASE_URL
    return urllib.parse.urlparse(url if "://" in url else "x://" + url).hostname or ""


def _patch_dns_for(host: str) -> None:
    try:
        _REAL_GETADDRINFO(host, 5432)
        return  # resolves fine, no patch needed
    except OSError:
        pass
    ips = _doh_ipv4(host)
    if not ips:
        raise RuntimeError(f"Could not resolve {host} via DoH either")
    print(f"DNS workaround: {host} -> {ips[0]} (DoH)", flush=True)

    def _patched(h, port, *args, **kwargs):
        if h == host:
            h = ips[0]  # connect by IP; TLS/SNI still uses the hostname
        return _REAL_GETADDRINFO(h, port, *args, **kwargs)

    socket.getaddrinfo = _patched


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    _patch_dns_for(_db_host())
    if mode == "initdb":
        import runpy

        sys.argv = ["app.init_db"]
        runpy.run_module("app.init_db", run_name="__main__")
    elif mode == "serve":
        import uvicorn

        uvicorn.run("app.main:app", host="127.0.0.1", port=8001, log_level="info")
    else:
        raise SystemExit(f"unknown mode: {mode}")
