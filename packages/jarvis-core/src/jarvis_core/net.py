"""How another machine reaches this one: its Bonjour name and its LAN address, best effort."""

from __future__ import annotations

import socket


def advertised_hosts() -> list[str]:
    """Names another Mac can reach this one by, best first; an IP is the fallback when mDNS does not answer."""
    hosts: list[str] = []
    name = socket.gethostname().split(".")[0]
    if name:
        hosts.append(f"{name}.local")
    try:
        hosts.append(socket.gethostbyname(name))
    except OSError:
        pass
    return list(dict.fromkeys(hosts))
