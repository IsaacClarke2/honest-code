#!/usr/bin/env python3
"""
State signing — makes state.json tamper-EVIDENT.

Honest threat model
-------------------
A PreToolUse regex hook (enforce.py) can always be bypassed: helper scripts,
`cd` into the directory, exotic interpreters, base64, etc. So we do NOT pretend
the score file is unmodifiable. Instead we SIGN it. If it is edited out-of-band,
the next run sees a broken signature, records a tamper incident, and penalizes
the score — the agent gets caught red-handed rather than silently winning.

Strength depends entirely on the key:

  * HONEST_CODE_KEY (or HONEST_CODE_KEY_FILE) set, and kept OUT of the agent's
    reach (a CI secret, a root-owned file, your shell rc that the agent can't
    read) -> HMAC-SHA256. The agent cannot forge a fresh signature, so any edit
    is detected. This is the real guarantee.

  * No key -> plain SHA-256. This only catches accidental corruption or a naive
    edit. A determined agent can recompute the hash itself. We say so plainly
    instead of pretending otherwise.

The signature lives inside state.json under the "signature" key and is excluded
from the signed payload, so no sidecar file is needed.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path

SIGNATURE_FIELD = "signature"


def _key():
    """Return the signing key as bytes, or None if no secret is configured."""
    k = os.environ.get("HONEST_CODE_KEY")
    if k:
        return k.encode("utf-8")
    kf = os.environ.get("HONEST_CODE_KEY_FILE")
    if kf:
        try:
            data = Path(kf).read_bytes().strip()
            if data:
                return data
        except OSError:
            pass
    return None


def is_keyed():
    """True if a real secret key is configured (HMAC mode)."""
    return _key() is not None


def _canonical(state):
    """Deterministic bytes for everything except the signature field itself."""
    payload = {k: v for k, v in state.items() if k != SIGNATURE_FIELD}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_signature(state):
    """Compute the signature for a state dict (HMAC if keyed, else SHA-256)."""
    data = _canonical(state)
    key = _key()
    if key:
        return "hmac-sha256:" + hmac.new(key, data, hashlib.sha256).hexdigest()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_signature(state):
    """True if the embedded signature matches the current payload."""
    sig = state.get(SIGNATURE_FIELD)
    if not sig:
        return False
    return hmac.compare_digest(compute_signature(state), str(sig))


def is_tampered(state):
    """True only if a signature is present AND invalid.

    A missing signature (first run, or a freshly initialised state) is NOT
    tampering — it just means the file has not been signed yet.
    """
    return bool(state.get(SIGNATURE_FIELD)) and not verify_signature(state)
