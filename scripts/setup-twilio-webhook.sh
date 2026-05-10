#!/bin/bash
# Setup Twilio webhook for Obscura

set -e

echo "🚀 Setting up Twilio webhook for Obscura"
echo ""

# Check for required env vars
if [ -z "$TWILIO_ACCOUNT_SID" ]; then
    echo "❌ TWILIO_ACCOUNT_SID not set"
    echo "   Get it from: https://console.twilio.com"
    exit 1
fi

if [ -z "$TWILIO_AUTH_TOKEN" ]; then
    echo "❌ TWILIO_AUTH_TOKEN not set"
    exit 1
fi

if [ -z "$TWILIO_PHONE_NUMBER" ]; then
    echo "❌ TWILIO_PHONE_NUMBER not set (e.g., +1234567890)"
    exit 1
fi

# Obscura gateway URL
OBSCURA_URL="${OBSCURA_URL:-http://localhost:18790}"
WEBHOOK_URL="$OBSCURA_URL/twilio/sms"

echo "✅ Configuration:"
echo "   Phone Number: $TWILIO_PHONE_NUMBER"
echo "   Webhook URL: $WEBHOOK_URL"
echo ""

# Install Twilio CLI if needed
if ! command -v twilio &> /dev/null; then
    echo "📦 Installing Twilio CLI..."
    npm install -g twilio-cli
fi

# Login to Twilio
echo "🔐 Logging into Twilio..."
twilio login "$TWILIO_ACCOUNT_SID" "$TWILIO_AUTH_TOKEN" || true

# Configure webhook
echo "🔗 Configuring webhook URL..."
twilio phone-numbers:update "$TWILIO_PHONE_NUMBER" \
    --sms-url="$WEBHOOK_URL" \
    --sms-method="POST" \
    --status-callback="$OBSCURA_URL/twilio/status" \
    --status-callback-method="POST"

echo ""
echo "✅ Twilio webhook configured!"
echo ""
echo "📱 Test it:"
echo "   Send SMS to: $TWILIO_PHONE_NUMBER"
echo "   It will POST to: $WEBHOOK_URL"
echo ""
echo "🔍 Check logs:"
echo "   tail -f ~/.obscura/logs/gateway.log"
