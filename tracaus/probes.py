"""Outside-process HTTPS checks with public-address pinning and no redirects/payload logs."""

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def check(route, *, resolve=socket.getaddrinfo, connection=PinnedHTTPS, clock=time.monotonic):
    parsed = urlsplit(route["url"])
    start = clock()
    conn = None
    try:
        addresses = {entry[4][0] for entry in resolve(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
            return {"healthy": False, "code": "DESTINATION_DENIED", "latency_ms": 0}
        # IPv4 first: a Lambda function has no IPv6 route out. The next address is tried only after a connection
        # error, so a server that answers badly is judged on its answer.
        ordered = sorted(
            addresses, key=lambda value: (ipaddress.ip_address(value).version, ipaddress.ip_address(value))
        )
        for address in ordered:
            left = route["timeout_seconds"] - (clock() - start)
            if left <= 0:
                return {"healthy": False, "code": "TIMEOUT", "latency_ms": 0}
            conn = connection(parsed.hostname, address, left)
            try:
                conn.request(
                    "GET", parsed.path, headers={"User-Agent": "Tracaus-Readiness/1", "Connection": "close"}
                )
                response = conn.getresponse()
                # A little of the body is read so a hung response shows as a timeout. It is not stored or judged.
                response.read(4096)
                break
            except OSError:
                conn.close()
                conn = None
        else:
            raise OSError("No address answered")
        latency = max(0, int((clock() - start) * 1000))
        healthy = response.status in route["statuses"] and latency <= route["latency_ms"]
        code = "OK" if healthy else "STATUS_OR_LATENCY"
        return {"healthy": healthy, "code": code, "latency_ms": latency}
    except Exception:
        return {
            "healthy": False,
            "code": "CONNECT_OR_READ_FAILED",
            "latency_ms": max(0, int((clock() - start) * 1000)),
        }
    finally:
        if conn:
            conn.close()


def fresh(points, now, maximum_age):
    """Missing/future/invalid timestamps cannot become healthy telemetry."""
    timestamps = [
        p.timestamp() for p in points if getattr(p, "tzinfo", None) is not None and p.utcoffset() is not None
    ]
    return bool(timestamps) and any(now - maximum_age <= t <= now + 5 for t in timestamps)


def bounded_check(route, *, launcher=None):
    """A killable child bounds DNS, slow headers and trickling reads together."""
    import json
    import subprocess
    import sys

    launcher = launcher or subprocess.Popen
    child = launcher(
        [sys.executable, "-m", "tracaus.probes"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        try:
            output, _ = child.communicate(json.dumps(route).encode(), timeout=route["timeout_seconds"])
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=1)
            return {"healthy": False, "code": "TIMEOUT", "latency_ms": route["timeout_seconds"] * 1000}
        if child.returncode != 0 or len(output) > 1024:
            raise ValueError("Probe subprocess failed")
        result = json.loads(output)
        if (
            set(result) != {"healthy", "code", "latency_ms"}
            or type(result["healthy"]) is not bool
            or type(result["latency_ms"]) is not int
            or result["latency_ms"] < 0
        ):
            raise ValueError("Invalid probe result")
        return result
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1)
        for pipe in (child.stdin, child.stdout):
            if pipe:
                pipe.close()


if __name__ == "__main__":
    import json
    import sys

    route = json.loads(sys.stdin.buffer.read(4097))
    print(json.dumps(check(route), separators=(",", ":")))
