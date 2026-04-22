# extension keys

**Private keys in this directory are gitignored** (`*.pem`, `*.key`). Do
not force-add them, do not share over chat, and do not paste into tickets.
If you suspect the key has leaked, regenerate immediately — the old id
becomes unsafe because anyone with the old `.pem` can sign a CRX that
Chrome treats as equivalent to the team's extension.

- `extension.pem` — RSA 2048 private key. **Never committed.** Only
  needed to sign a `.crx` (we distribute unpacked, so you don't usually
  touch it). Store an offline backup if you care about a stable id.
- `extension.pub.b64` — base64-encoded DER public key. This is what gets
  pasted into `manifest.json` as the `key` field. Safe to commit (it's
  what gives the unpacked extension a stable id).
- `EXTENSION_ID` — the stable Chrome extension id derived from the
  public key: `sha256(der)[0:32]` with each hex nibble mapped `0..f → a..p`.
  The installer reads this file by default.

To regenerate (new id):

```bash
openssl genrsa -out extension.pem 2048
openssl rsa -in extension.pem -pubout -outform DER | base64 | tr -d '\n' > extension.pub.b64
python3 -c "
import hashlib, base64
der = base64.b64decode(open('extension.pub.b64').read().strip())
h = hashlib.sha256(der).hexdigest()[:32]
print(''.join(chr(ord('a') + int(c, 16)) for c in h))
" > EXTENSION_ID
```

Then paste the contents of `extension.pub.b64` into
`../manifest.json` → `"key"`.
