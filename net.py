"""Shared HTTP session for every outbound call ANVI makes."""

import ssl

import requests
from requests.adapters import HTTPAdapter

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


http = requests.Session()
http.mount("https://", _SystemTrustAdapter(pool_maxsize=20))
