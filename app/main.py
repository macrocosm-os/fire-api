from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import Any
import json
import httpx
import time
import asyncio  
import bittensor as bt
from types import SimpleNamespace
from .utils import get_top_miners, generate_header
from .config import Config as config

@dataclass
class BTResources:
    subtensor: bt.subtensor
    wallet: bt.wallet

def get_resources(request: Request) -> BTResources:
    return BTResources(
        subtensor=request.app.state.subtensor,
        wallet=request.app.state.wallet,
    )

# Simple in-memory cache for metagraph with 5-minute TTL
CACHE_TTL_SECONDS = 300

def get_metagraph_cached(app: FastAPI, subtensor: bt.subtensor):
    last_ts: float = getattr(app.state, "metagraph_ts", 0.0)
    cached_value = getattr(app.state, "metagraph_value", None)
    now = time.time()

    if cached_value is None or (now - last_ts) >= CACHE_TTL_SECONDS:
        # Refresh cache
        cached_value = subtensor.metagraph(1)
        app.state.metagraph_value = cached_value
        app.state.metagraph_ts = now

    return cached_value

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Instantiate once per process
    app.state.subtensor = bt.subtensor(config.network)
    app.state.wallet = bt.wallet(name=config.wallet, hotkey=config.hotkey)
    # Initialize metagraph cache placeholders
    app.state.metagraph_value = None
    app.state.metagraph_ts = 0.0
    # Optional webhook URL for forwarding successful responses
    app.state.forward_webhook_url = config.scoring_url
    try:
        yield
    finally:
        pass
        

async def _post_to_miner(
    client: httpx.AsyncClient,
    miner: SimpleNamespace,
    payload: dict[str, Any],
    timeout_seconds: float,
    wallet: bt.wallet,
) -> tuple[SimpleNamespace, httpx.Response, Exception | None]:
        """Query a single miner using the provided HTTP client."""
        try:
            headers = await generate_header(
                wallet.hotkey, body=json.dumps(payload).encode("utf-8"), signed_for=miner.hotkey
            )
            resp = await client.post(
                f"{miner.address}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout_seconds,
            )
            resp.raise_for_status()
            return miner, resp, None
        except Exception as e:
            return miner, None, e


async def post_to_miners_first(
    miners: list[SimpleNamespace],
    payload: dict[str, Any],
    wallet: bt.wallet,
    per_request_timeout: float = 2.0,
    overall_timeout: float = 5.0,
):
    async with httpx.AsyncClient() as client:
        tasks = [
            asyncio.create_task(
                _post_to_miner(client, miner, payload, per_request_timeout, wallet)
            )
            for miner in miners
        ]
        try:
            for coro in asyncio.as_completed(tasks, timeout=overall_timeout):
                miner, response, error = await coro
                if response is not None and response.status_code < 400:
                    # Cancel remaining tasks
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    return miner, response
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
    return None, None


def _serialize_miner(miner: SimpleNamespace) -> dict[str, Any]:
    endpoint_ip = getattr(getattr(miner, "endpoint", None), "ip", None)
    endpoint_port = getattr(getattr(miner, "endpoint", None), "port", None)
    return {
        "hotkey": getattr(miner, "hotkey", None),
        "address": getattr(miner, "address", None),
        "endpoint_ip": endpoint_ip,
        "endpoint_port": endpoint_port,
    }


async def _forward_webhook(webhook_url: str, miner: SimpleNamespace, prompt: str, upstream_response: httpx.Response) -> None:
    try:
        payload: dict[str, Any] = {
            "miner": _serialize_miner(miner),
            "prompt": prompt,
            "response": {
                "status_code": upstream_response.status_code,
                "content_type": upstream_response.headers.get("content-type"),
                "text": upstream_response.text,
            },
        }
        async with httpx.AsyncClient() as client:
            await client.post(webhook_url, json=payload, timeout=3.0)
    except Exception:
        # Intentionally swallow errors to avoid impacting the main request flow
        pass


def create_application() -> FastAPI:
    app = FastAPI(title="Fire API", version="0.1.0", lifespan=lifespan)

    # Root health endpoint
    @app.get("/health", summary="Healthcheck", tags=["health"])
    async def healthcheck() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # Metrics endpoint (stub)
    @app.get("/metrics", summary="Metrics (stub)", tags=["metrics"], response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        # Placeholder metrics output. Integrate prometheus_client in the future as needed.
        return PlainTextResponse("# Metrics are not implemented yet\n")

    # API v1 router with a stubbed completions endpoint
    api_v1 = APIRouter(prefix="/v1", tags=["v1"])

    @api_v1.post("/completions", summary="Create a completion (stub)")
    async def create_completion(
        request: Request,
        prompt: str | None = None,
        resources: BTResources = Depends(get_resources),
    ) -> Response:
        if prompt is None:
            raise HTTPException(status_code=400, detail="Missing 'prompt' in request")
        metagraph = get_metagraph_cached(request.app, resources.subtensor)
        miners = get_top_miners(metagraph, 5)

        # Fan out POST requests to miners and return the first successful response
        payload = {"prompt": prompt}
        miner, upstream_response = await post_to_miners_first(miners, payload, resources.wallet)

        if upstream_response is None:
            raise HTTPException(status_code=502, detail="No miners responded successfully in time")

        # Fire-and-forget forward of miner/prompt/response to webhook if configured
        webhook_url: str | None = getattr(request.app.state, "forward_webhook_url", None)
        if webhook_url:
            asyncio.create_task(_forward_webhook(webhook_url, miner, prompt, upstream_response))

        # Proxy the upstream response back to the client
        content_type = upstream_response.headers.get("content-type", "application/octet-stream")
        return Response(content=upstream_response.content, media_type=content_type)

    app.include_router(api_v1)

    return app


app = create_application()


if __name__ == "__main__":
    # For local development only. In Docker, use the provided CMD.
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)


