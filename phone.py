"""Phone access for Karen: HTTPS on the local network + QR-code pairing.

Phone browsers only allow the microphone on HTTPS pages, so a second server
listens on the LAN with a self-signed certificate. Karen can control this PC,
so every request from another device must carry the pairing cookie, which a
phone gets by scanning the QR code shown on the PC.
"""

import datetime
import hmac
import ipaddress
import os
import secrets
import socket
from pathlib import Path

DATA_DIR = Path(os.getenv("ANVI_HOME") or Path(__file__).resolve().parent) / ".anvi"
SECRET_FILE = DATA_DIR / "pairing_secret"
CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE = DATA_DIR / "key.pem"
COOKIE_NAME = "anvi_pair"


def lan_ip() -> str:
    """The address other devices on the Wi-Fi reach this PC on (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def pairing_secret() -> str:
    """Persistent random secret; delete .anvi/pairing_secret to unpair every phone."""
    DATA_DIR.mkdir(exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip()


def is_paired(token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, pairing_secret())


def ensure_certificate(ip: str) -> tuple[str, str]:
    """Self-signed cert for this PC's LAN IP, regenerated when the IP changes."""
    DATA_DIR.mkdir(exist_ok=True)
    ips = sorted({ip, "127.0.0.1"})
    from cryptography import x509

    # The addresses live inside the certificate itself. (A separate .txt file used to hold them, but
    # antivirus ransomware protection can block an unknown app from changing .txt files.)
    if CERT_FILE.exists() and KEY_FILE.exists():
        try:
            existing = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if sorted(str(a) for a in san.get_values_for_type(x509.IPAddress)) == ips:
                return str(CERT_FILE), str(KEY_FILE)
        except (ValueError, x509.ExtensionNotFound):
            pass  # unreadable or old certificate: make a new one
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Karen")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(
            [x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
            + [x509.IPAddress(ipaddress.ip_address(a)) for a in ips]
        ), critical=False)
        .sign(key, hashes.SHA256())
    )
    KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(CERT_FILE), str(KEY_FILE)
