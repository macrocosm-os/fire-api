import random
from typing import Any
import time
from math import ceil
from hashlib import sha256
from uuid import uuid4
from substrateinterface import Keypair
from types import SimpleNamespace


def get_top_miners(metagraph, n) -> SimpleNamespace:
    incentives = metagraph.I
    # Get indices sorted by incentive values in descending order
    sorted_indices = sorted(range(len(incentives)), key=lambda i: incentives[i], reverse=True)
    
    # Get the top 50% of miners
    top_50_percent_count = len(sorted_indices) // 2
    top_miners_indices = sorted_indices[:top_50_percent_count]
    top_miner_uids = random.sample(top_miners_indices, n)

    top_miner_hotkeys = [metagraph.hotkeys[i] for i in top_miner_uids]
    top_miner_endpoints = [metagraph.axons[i] for i in top_miner_uids]
    top_miner_addresses = [f"http://{metagraph.axons[idx].ip}:{metagraph.axons[idx].port}" for idx in top_miner_uids]
    miners = [SimpleNamespace(hotkey=hotkey, endpoint=endpoint, address=address) for hotkey, endpoint, address in zip(top_miner_hotkeys, top_miner_endpoints, top_miner_addresses)]
    return miners

async def generate_header(
    hotkey: Keypair,
    body: bytes,
    signed_for: str | None = None,
) -> dict[str, Any]:
    timestamp = round(time.time() * 1000)
    timestamp_interval = ceil(timestamp / 1e4) * 1e4
    uuid = str(uuid4())
    headers = {
        "Epistula-Version": "2",
        "Epistula-Timestamp": str(timestamp),
        "Epistula-Uuid": uuid,
        "Epistula-Signed-By": hotkey.ss58_address,
        "Epistula-Request-Signature": "0x"
        + hotkey.sign(f"{sha256(body).hexdigest()}.{uuid}.{timestamp}.{signed_for or ''}").hex(),
    }
    if signed_for:
        headers["Epistula-Signed-For"] = signed_for
        headers["Epistula-Secret-Signature-0"] = (
            "0x" + hotkey.sign(str(timestamp_interval - 1) + "." + signed_for).hex()
        )
        headers["Epistula-Secret-Signature-1"] = "0x" + hotkey.sign(str(timestamp_interval) + "." + signed_for).hex()
        headers["Epistula-Secret-Signature-2"] = (
            "0x" + hotkey.sign(str(timestamp_interval + 1) + "." + signed_for).hex()
        )
    return headers