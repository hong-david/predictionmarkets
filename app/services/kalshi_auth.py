import base64
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key(key_path: str):
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def load_private_key_from_pem(private_key_pem: str):
    return serialization.load_pem_private_key(
        private_key_pem.replace("\\n", "\n").encode("utf-8"),
        password=None,
    )


def create_signature(private_key, timestamp: str, method: str, path: str) -> str:
    path_without_query = path.split("?")[0]
    message = f"{timestamp}{method}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def create_ws_headers(
    api_key_id: str,
    private_key_path: str,
    private_key_pem: str = "",
) -> dict[str, str]:
    private_key = (
        load_private_key_from_pem(private_key_pem)
        if private_key_pem
        else load_private_key(private_key_path)
    )
    timestamp = str(int(time.time() * 1000))
    signature = create_signature(private_key, timestamp, "GET", "/trade-api/ws/v2")

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
