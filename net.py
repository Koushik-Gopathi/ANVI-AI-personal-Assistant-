"""Shared HTTP session for every outbound call Karen makes."""

import ssl

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


# Antivirus HTTPS scanning (Avast/AVG "Web Shield") re-signs every TLS
# connection with its own root CA. Windows trusts that CA but Python's
# certifi bundle does not, so every API call failed with
# CERTIFICATE_VERIFY_FAILED. Use the Windows certificate store instead, and
# relax Python 3.13+'s strict X.509 mode, which rejects the Avast root.
class _SystemTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# Flaky Wi-Fi (and antivirus scanning) sometimes stalls a connection for a few
# seconds. Retry a failed connect or a request that got no response at all, with
# a short growing pause, before giving up. Karen's POSTs (speech, chat, voice) are
# safe to repeat. Status codes are not retried here: rate limits are handled in main.py.
RETRIES = Retry(total=3, connect=3, read=2, status=0, other=1, backoff_factor=0.8,
                allowed_methods=frozenset({"GET", "POST"}), raise_on_status=False)

http = requests.Session()
http.mount("https://", _SystemTrustAdapter(pool_maxsize=20, max_retries=RETRIES))
http.mount("http://", HTTPAdapter(max_retries=RETRIES))


def friendly_error(e: Exception) -> str:
    """Short, human message for network failures instead of a raw exception dump."""
    if isinstance(e, requests.Timeout):
        return "The internet is slow right now and the request timed out. Please try again."
    if isinstance(e, requests.ConnectionError):
        return "I couldn't reach the internet. Check the Wi-Fi and try again."
    return str(e)
