"""
generate_certs.py
-----------------
Generates a self-signed X.509 certificate and RSA private key for the
TLS control channel used by the Jackfruit Notification Server.

Run:
    python generate_certs.py
Output:
    server.crt  – PEM-encoded certificate (shared with clients as CA)
    server.key  – PEM-encoded private key  (keep on server only)
"""

import datetime
import ipaddress
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_self_signed_cert(
    cert_file: str = "server.crt",
    key_file: str = "server.key",
) -> None:
    # ------------------------------------------------------------------ #
    # 1. Generate 2048-bit RSA private key                                 #
    # ------------------------------------------------------------------ #
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # ------------------------------------------------------------------ #
    # 2. Build certificate subject / issuer (self-signed → same)          #
    # ------------------------------------------------------------------ #
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "IN"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Tamil Nadu"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Jackfruit Notification System"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )

    # ------------------------------------------------------------------ #
    # 3. Sign with SHA-256, valid for 1 year                              #
    # ------------------------------------------------------------------ #
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    # ------------------------------------------------------------------ #
    # 4. Write PEM files                                                   #
    # ------------------------------------------------------------------ #
    with open(key_file, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"[OK] Private key  → {key_file}")
    print(f"[OK] Certificate  → {cert_file}")
    print(f"     Subject      : {cert.subject.rfc4514_string()}")
    print(f"     Valid until  : {cert.not_valid_after}")
    print()
    print("Copy server.crt to each client machine (used as CA to verify the server).")


if __name__ == "__main__":
    generate_self_signed_cert()
