from fastapi.testclient import TestClient
from obscura.server.internal_rpc import app

client = TestClient(app)

def test_handshake():
    r = client.post('/internal/handshake', json={'pubkey':'test'})
    assert r.status_code == 200
    data = r.json()
    assert 'backgroundPubKey' in data
    assert isinstance(data['backgroundPubKey'], str)

def test_invoke():
    r = client.post('/internal/invoke', json={'function_name':'fn','full_payload':{}})
    assert r.status_code == 200
    assert r.json().get('status_code') == 202
