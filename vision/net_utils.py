"""
[WIRED] Small standalone helper — no hardware/broker dependency itself.

Used by Middleman mode (see vision/services/middleman_*.py) to identify
a Physical Side machine by its LAN IP. This IP is only ever a *label* —
both sides still only talk to the shared MQTT broker
(MQTT_BROKER_HOST/PORT in vision/config.py), never to each other
directly — it's used to namespace that Physical Side's topics
(arm/middleman/{ip}/...) so multiple Physical Side/Other Side pairs on
the same broker don't cross-talk.
"""

from __future__ import annotations
import socket


def get_local_ip(probe_host: str = "8.8.8.8", probe_port: int = 80) -> str:
    """
    Best-effort local IP detection: opens a throwaway UDP "connection"
    toward `probe_host` (nothing is actually sent) purely so the OS picks
    a real outbound interface, then reads back which local address it
    chose. More reliable than hostname lookups on machines with multiple
    network interfaces (Wi-Fi + Ethernet + VPN, etc.).

    Defaults to probing toward a public IP, but callers doing this for
    Middleman mode should generally pass the actual MQTT broker's
    host/port instead, so the interface chosen is the one that can
    actually reach the broker.

    Falls back to "127.0.0.1" if detection fails outright (no network,
    sandboxed environment, etc.) rather than raising — callers should
    treat that as "needs a manual override", not a hard failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((probe_host, probe_port))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
