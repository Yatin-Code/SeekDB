from __future__ import annotations

import hashlib
import os
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from seekdb.auth import DeviceAuth
from seekdb.index.sqlite import SQLiteIndex
from seekdb.pairing import InviteManager, PairingClosed, PairingManager, derive_key, client_proof
from seekdb.tls import ensure_certificate


def test_generated_tls_identity_is_private_stable_and_pinnable(tmp_path):
    cert_path, key_path, pin = ensure_certificate(tmp_path)
    cert_bytes, key_bytes = cert_path.read_bytes(), key_path.read_bytes()
    cert = x509.load_pem_x509_certificate(cert_bytes)
    key = serialization.load_pem_private_key(key_bytes, password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) == \
        key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    import base64
    assert pin == "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode()
    assert ensure_certificate(tmp_path) == (cert_path, key_path, pin)
    if os.name == "posix":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(cert_path.parent.stat().st_mode) == 0o700


def test_invite_pairing_issues_revocable_device_token_and_burns_bad_proofs(tmp_path):
    index = SQLiteIndex(tmp_path / "seekdb.db")
    try:
        auth = DeviceAuth(index)
        pin = ensure_certificate(tmp_path)[2]
        invites = InviteManager(index, pin, auth.register, iterations=200_000)
        pairing = PairingManager(pin, auth.register, invites=invites, iterations=200_000)
        pairing.sharing_enabled = lambda: True
        invitation = invites.create(role="contributor", max_uses=1)
        code = invitation["code"].replace("-", "")
        start = pairing.start(code[:4])
        key = derive_key(code[4:], start["pair_id"], start["salt"], start["spki_pin"], start["iterations"])
        name = "Nuvio TV"
        proof = client_proof(key, start["pair_id"], name)
        result = pairing.claim(start["pair_id"], name, proof)

        assert result["role"] == "contributor"
        assert auth.authenticate(f"Bearer {result['token']}") == result["device_id"]
        assert index.q("SELECT token_hash FROM devices WHERE device_id=?", (result["device_id"],))[0][0] == \
            hashlib.sha256(result["token"].encode()).hexdigest()
        assert result["token"] not in repr(index.q("SELECT token_hash FROM devices"))
        assert "code" not in index.invite(invitation["invite_id"])

        try:
            pairing.start(code[:4])
        except PairingClosed:
            pass
        else:
            raise AssertionError("used invite should be closed")

        assert auth.revoke(result["device_id"])
        assert auth.authenticate(f"Bearer {result['token']}") is None
    finally:
        index.close()

