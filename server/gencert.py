"""
gencert.py — generate a CA + server cert for the HTTPS bootstrap stub.

The client fetches its server list from https://*.noyagame.com / *.onelinkmobi.com
(now dead, redirected to our PC via the device hosts file). On Android 9 an app
targeting SDK 28 trusts only SYSTEM CAs, so we:
  1) generate a CA  -> install ca.pem into the device system trust store
  2) generate a leaf cert (SAN = those domains) signed by the CA -> bootstrap.py serves it

Outputs (next to this file): ca.pem, ca.key, cert.pem, key.pem
ca.pem is also copied to <subject_hash_old>.0 (the filename Android expects in
/system/etc/security/cacerts/); the hash is printed for the install step.
"""

from __future__ import annotations

import datetime
import os
import subprocess

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HERE = os.path.dirname(os.path.abspath(__file__))

# Domains the client contacts (from Resources/luascript/area_info.txt + servermgr).
SAN_DNS = [
    "api.ttus.noyagame.com", "data.ttus.noyagame.com",
    "api.tten.noyagame.com", "data.tten.noyagame.com",
    "ttapi.onelinkmobi.com", "ttdata.tten.onelinkmobi.com",
    "*.noyagame.com", "*.onelinkmobi.com",
]


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _save(path, data):
    with open(path, "wb") as f:
        f.write(data)


def main():
    now = datetime.datetime.utcnow()
    ten_years = now + datetime.timedelta(days=3650)

    # --- CA ---
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LoD Local Dev CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(ten_years)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                     crl_sign=True, key_encipherment=False,
                                     content_commitment=False, data_encipherment=False,
                                     key_agreement=False, encipher_only=False,
                                     decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    # --- leaf signed by CA ---
    leaf_key = _key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.ttus.noyagame.com")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(ten_years)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(d) for d in SAN_DNS]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    pem = serialization.Encoding.PEM
    nokey = serialization.NoEncryption()
    _save(os.path.join(HERE, "ca.pem"), ca_cert.public_bytes(pem))
    _save(os.path.join(HERE, "ca.key"), ca_key.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL, nokey))
    _save(os.path.join(HERE, "cert.pem"), leaf_cert.public_bytes(pem))
    _save(os.path.join(HERE, "key.pem"), leaf_key.private_bytes(pem, serialization.PrivateFormat.TraditionalOpenSSL, nokey))

    # Android system-cacert filename = <subject_hash_old>.0
    try:
        h = subprocess.check_output(
            ["openssl", "x509", "-inform", "PEM", "-subject_hash_old", "-in", os.path.join(HERE, "ca.pem")],
            text=True).splitlines()[0].strip()
        _save(os.path.join(HERE, f"{h}.0"), ca_cert.public_bytes(pem))
        print(f"generated ca.pem, cert.pem (+ key files). android system-cert file: {h}.0")
    except Exception as e:
        print(f"generated ca.pem, cert.pem (+ key files). (couldn't compute android hash: {e})")


if __name__ == "__main__":
    main()
