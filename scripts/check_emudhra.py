"""One-time check for the eMudhra CA incident (2026-10-21).

Runs inside the built image. Exit 0 = not vulnerable, 1 = vulnerable.
A certificate error on the test hosts is the GOOD outcome.
Source: https://emudhra.we-have-a-problem.com/
"""
import socket
import ssl
import sys

import certifi
import requests

MIN_CERTIFI = (2026, 10, 19)
HOSTS = (
    "dv-g3b-emudhra.we-have-a-problem.com",
    "dv-g3b-aaa-emudhra.we-have-a-problem.com",
)

failed = False

version = tuple(int(p) for p in certifi.__version__.split(".")[:3])
print(f"certifi {certifi.__version__}")
if version < MIN_CERTIFI:
    print("FAIL: certifi older than 2026.10.19")
    failed = True

for host in HOSTS:
    # requests path (certifi bundle)
    try:
        requests.get(f"https://{host}/", timeout=15)
        print(f"FAIL requests {host}: connection accepted (vulnerable)")
        failed = True
    except requests.exceptions.SSLError:
        print(f"OK   requests {host}: certificate rejected")
    except requests.exceptions.RequestException as exc:
        print(f"FAIL requests {host}: inconclusive ({type(exc).__name__})")
        failed = True

    # ssl path (system CA store)
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                print(f"FAIL ssl {host}: connection accepted (vulnerable)")
                failed = True
    except ssl.SSLCertVerificationError:
        print(f"OK   ssl      {host}: certificate rejected")
    except OSError as exc:
        print(f"FAIL ssl {host}: inconclusive ({type(exc).__name__})")
        failed = True

sys.exit(1 if failed else 0)
