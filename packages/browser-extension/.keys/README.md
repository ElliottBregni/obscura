# extension keys

**Public material only.** This directory holds `EXTENSION_ID` and
`extension.pub.b64` — both safe to commit. The stable extension id is
derived from the public key.

**The private key (`extension.pem`) lives OUTSIDE this directory** at
`../../browser-extension-keys/extension.pem` (a sibling of
`packages/browser-extension/`). Chrome's unpacked loader scans every file
inside the extension folder and emits a security warning if it finds the
`.pem` — hence the split.

- `extension.pub.b64` — base64-encoded DER public key. Pasted into
  `manifest.json` as the `key` field. Gives the unpacked extension a
  stable id across reloads.
- `EXTENSION_ID` — the stable Chrome extension id derived from the
  public key: `sha256(der)[0:32]` with each hex nibble mapped `0..f → a..p`.
  The installer reads this file by default.

Rotating keys (new extension id):

```bash
# Run from packages/browser-extension/.keys
PRIV=../../browser-extension-keys/extension.pem
mkdir -p "$(dirname "$PRIV")"
openssl genrsa -out "$PRIV" 2048
openssl rsa -in "$PRIV" -pubout -outform DER | base64 | tr -d '\n' > extension.pub.b64
python3 -c "
import hashlib, base64
der = base64.b64decode(open('extension.pub.b64').read().strip())
h = hashlib.sha256(der).hexdigest()[:32]
print(''.join(chr(ord('a') + int(c, 16)) for c in h))
" > EXTENSION_ID
```

Then paste the contents of `extension.pub.b64` into
`../manifest.json` → `"key"`. **Never move the `.pem` back into this
directory** — Chrome will warn again and a bad pack script could ship it.
