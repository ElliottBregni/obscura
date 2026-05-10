"""Twilio webhook integration for Obscura.

Receives SMS/MMS via Twilio webhooks and routes to Obscura sessions.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from obscura.gateway.messaging_bridge import MessagingSessionBridge
from obscura.integrations.messaging.models import PlatformMessage
from datetime import datetime, UTC

logger = logging.getLogger(__name__)


class TwilioWebhookHandler:
    """Handle Twilio incoming SMS/MMS webhooks."""
    
    def __init__(self, messaging_bridge: MessagingSessionBridge) -> None:
        self.bridge = messaging_bridge
        self.auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        
    async def handle_incoming_sms(self, request: Request) -> Response:
        """Handle incoming SMS from Twilio.
        
        Form data from Twilio:
        - From: sender phone number
        - To: Twilio phone number  
        - Body: message text
        - MessageSid: unique message ID
        - NumMedia: number of media attachments
        """
        try:
            form = await request.form()
            
            from_number = form.get("From", "")
            to_number = form.get("To", "")
            body = form.get("Body", "")
            message_sid = form.get("MessageSid", "")
            num_media = int(form.get("NumMedia", 0))
            
            logger.info(f"Received SMS from {from_number}: {body[:50]}...")
            
            # Create platform message
            message = PlatformMessage(
                platform="twilio",
                account_id="default",
                channel_id=from_number,
                sender_id=from_number,
                recipient_id=to_number,
                message_id=message_sid,
                text=body,
                timestamp=datetime.now(UTC),
                metadata={
                    "to": to_number,
                    "num_media": num_media,
                }
            )
            
            # Process through bridge
            response_text = await self.bridge.handle_message(message)
            
            # Return TwiML response
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{response_text}</Message>
</Response>"""
            
            return Response(
                content=twiml,
                media_type="application/xml"
            )
            
        except Exception as e:
            logger.error(f"Error handling Twilio webhook: {e}")
            # Return empty response on error
            return Response(
                content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
                media_type="application/xml"
            )
    
    async def handle_status_callback(self, request: Request) -> Response:
        """Handle Twilio message status callbacks."""
        form = await request.form()
        message_sid = form.get("MessageSid", "")
        status = form.get("MessageStatus", "")
        
        logger.debug(f"Message {message_sid} status: {status}")
        
        return PlainTextResponse("OK")


def create_twilio_router(messaging_bridge: MessagingSessionBridge) -> APIRouter:
    """Create FastAPI router for Twilio webhooks."""
    router = APIRouter(prefix="/twilio", tags=["Twilio"])
    handler = TwilioWebhookHandler(messaging_bridge)
    
    @router.post("/sms")
    async def incoming_sms(request: Request) -> Response:
        """Receive incoming SMS from Twilio."""
        return await handler.handle_incoming_sms(request)
    
    @router.post("/status")
    async def status_callback(request: Request) -> Response:
        """Receive message status callbacks from Twilio."""
        return await handler.handle_status_callback(request)
    
    @router.get("/health")
    async def health_check() -> dict[str, Any]:
        """Health check endpoint."""
        return {"status": "ok", "service": "twilio-webhook"}
    
    return router
