"""Opt-in client for the isolated loopback admission harness, not a runtime route.

Tokens are supplied by the owner in memory. This client never resolves profiles,
reads credentials, follows redirects, or enables the gateway dispatcher.
"""

import http.client
import json

from hermes_cli.kanban_worker_bridge import AdmissionRejected


class LoopbackQueue:
    """Read/admit port; the server must bind the capability to one mission."""

    def __init__(self, port: int, token: str, timeout: float = 7):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("INVALID_PORT")
        if (
            not isinstance(token, str)
            or not 16 <= len(token) <= 512
            or any(char.isspace() or ord(char) > 127 for char in token)
        ):
            raise ValueError("INVALID_CAPABILITY")
        if not 0 < timeout <= 30:
            raise ValueError("INVALID_TIMEOUT")
        self.port, self._token, self.timeout = port, token, timeout

    def _call(self, body):
        encoded = json.dumps(body).encode()
        if len(encoded) > 32768:
            raise ValueError("REQUEST_TOO_LARGE")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            conn.request(
                "POST",
                "/fixture",
                encoded,
                {
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            data = response.read(65537)
            if len(data) > 65536:
                raise RuntimeError("QUEUE_OUTCOME_UNCERTAIN")
            # These codes mean pre-write rejection in the isolated API contract.
            # Unknown status, redirect, broken response or timeout is uncertain.
            if response.status in (400, 401, 403):
                raise AdmissionRejected("QUEUE_REJECTED")
            if response.status != 200:
                raise RuntimeError("QUEUE_OUTCOME_UNCERTAIN")
            return json.loads(data)
        finally:
            conn.close()

    def read(self, request_id):
        return self._call({"op": "read", "requestId": request_id})

    def admit(self, request_id, payload):
        return self._call({"op": "admit", "requestId": request_id, "payload": payload})
