#!/bin/bash
# Test Twilio webhook locally

OBSCURA_URL="${OBSCURA_URL:-http://localhost:18790}"

echo "🧪 Testing Twilio webhook at $OBSCURA_URL"
echo ""

# Test health endpoint
echo "1. Testing health endpoint..."
curl -s "$OBSCURA_URL/twilio/health" | jq .
echo ""

# Test SMS webhook (simulate Twilio POST)
echo "2. Testing SMS webhook..."
curl -s -X POST "$OBSCURA_URL/twilio/sms" \
  -d "From=%2B12316333624" \
  -d "To=%2B15551234567" \
  -d "Body=Hello%20from%20Twilio" \
  -d "MessageSid=SM123456789" \
  -d "NumMedia=0"
echo ""
echo ""

# Check if Obscura gateway is running
echo "3. Checking Obscura gateway..."
curl -s "$OBSCURA_URL/health" | jq .
echo ""

echo "✅ Test complete!"
