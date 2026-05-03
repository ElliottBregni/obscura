from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import base64
import os

app = FastAPI()


class HandshakeReq(BaseModel):
    pubkey: str


@app.post("/internal/handshake")
async def handshake(req: HandshakeReq):
    # Return a simple ephemeral backgroundPubKey (base64 random bytes).
    b = os.urandom(65)
    return {"backgroundPubKey": base64.b64encode(b).decode()}


class InvokeReq(BaseModel):
    function_name: str
    full_payload: dict[str, Any]


@app.post("/internal/invoke")
async def invoke(req: InvokeReq):
    # Accept the invoke and return a 202-like payload for compatibility with client
    return {"status_code": 202}
