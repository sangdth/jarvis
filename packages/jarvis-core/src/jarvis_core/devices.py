"""
Paired devices: one secret per client, so a laptop can connect without pasting the shared token.

`jarvis pair` shows a short code; the other machine trades it for its own secret, which it keeps in
`~/.config/jarvis-remote/credentials.json` (0600). The store here keeps only a SHA-256 of each secret, so
`JARVIS_HOME/devices.json` is not itself a credential file, and revoking one device leaves the others working.

The shared token (`brain_token`) still works for the CLI and for scripts; pairing is the second credential, not a
replacement.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from jarvis_core.home import JARVIS_HOME

DEVICES_FILE = "devices.json"
# A device's `last_seen` is written at most this often: a status poll must not rewrite the file every second.
LAST_SEEN_EVERY_SECS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_secret(secret: str) -> str:
    """What goes on disk: the store never holds a usable secret."""
    return hashlib.sha256(secret.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: str
    last_seen: str | None = None
    revoked: bool = False
    secret_sha256: str = ""

    def public(self) -> dict:
        """What a client may see: no hash, no secret."""
        data = asdict(self)
        data.pop("secret_sha256", None)
        return data


class DeviceStore:
    """The paired devices, loaded once and written atomically; one instance is shared by every app in the process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._devices: dict[str, Device] = self._read()

    @classmethod
    def load(cls, path: Path | None = None) -> "DeviceStore":
        return cls(path or (JARVIS_HOME / DEVICES_FILE))

    def _read(self) -> dict[str, Device]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        devices: dict[str, Device] = {}
        for entry in raw.get("devices", []) if isinstance(raw, dict) else []:
            try:
                device = Device(**entry)
            except (TypeError, ValueError):
                continue
            devices[device.id] = device
        return devices

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"devices": [asdict(d) for d in self._devices.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def add(self, name: str, secret: str | None = None) -> tuple[Device, str]:
        """Register a device and return it with its secret (the only time the secret exists in the clear)."""
        plain = secret or secrets.token_urlsafe(32)
        label = " ".join((name or "").split())[:60] or "A device"
        with self._lock:
            device = Device(
                id=f"dev-{secrets.token_hex(4)}",
                name=label,
                created_at=_iso(_now()),
                secret_sha256=hash_secret(plain),
            )
            self._devices[device.id] = device
            self._save()
        return device, plain

    def verify(self, secret: str) -> Device | None:
        """The device this secret belongs to, or None when it is unknown or was revoked."""
        token = (secret or "").strip()
        if not token:
            return None
        digest = hash_secret(token)
        with self._lock:
            for device in self._devices.values():
                if device.revoked or not hmac.compare_digest(device.secret_sha256, digest):
                    continue
                seen = _now()
                if not device.last_seen or (seen - _parse(device.last_seen)).total_seconds() >= LAST_SEEN_EVERY_SECS:
                    device = replace(device, last_seen=_iso(seen))
                    self._devices[device.id] = device
                    self._save()
                return device
        return None

    def devices(self) -> list[Device]:
        with self._lock:
            return sorted(self._devices.values(), key=lambda d: d.created_at, reverse=True)

    def revoke(self, device_id: str) -> Device | None:
        """Revoke one device; the rest keep working. Unknown id: None."""
        with self._lock:
            device = self._devices.get(device_id.strip())
            if device is None:
                return None
            revoked = replace(device, revoked=True)
            self._devices[revoked.id] = revoked
            self._save()
            return revoked


def _parse(stamp: str) -> datetime:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return _now()
