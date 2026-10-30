"""Contract tests using a connection double; no listener or secret is created."""

import json
from unittest.mock import patch

import pytest

from hermes_cli.kanban_worker_bridge import AdmissionRejected
from hermes_cli.kanban_worker_transport import LoopbackQueue


class Connection:
    def __init__(self, status, body=b"null"):
        self.status, self.body, self.closed = status, body, False
        self.calls = []

    def request(self, *args):
        self.calls.append(args)

    def getresponse(self):
        return self

    def read(self, length):
        return self.body[:length]

    def close(self):
        self.closed = True


def test_loopback_only_and_valid_receipt():
    conn = Connection(200, b'{"jobId":"synthetic"}')
    with patch(
        "hermes_cli.kanban_worker_transport.http.client.HTTPConnection",
        return_value=conn,
    ) as factory:
        result = LoopbackQueue(12345, "synthetic-capability").admit(
            "mission", {"run": 1}
        )
    factory.assert_called_once_with("127.0.0.1", 12345, timeout=7)
    assert result["jobId"] == "synthetic" and conn.closed
    assert json.loads(conn.calls[0][2])["requestId"] == "mission"


@pytest.mark.parametrize(
    "status,exception",
    [
        (400, AdmissionRejected),
        (401, AdmissionRejected),
        (403, AdmissionRejected),
        (302, RuntimeError),
        (500, RuntimeError),
    ],
)
def test_uncertain_is_never_confirmed_rejection(status, exception):
    conn = Connection(status)
    with patch(
        "hermes_cli.kanban_worker_transport.http.client.HTTPConnection",
        return_value=conn,
    ):
        with pytest.raises(exception):
            LoopbackQueue(12345, "synthetic-capability").read("mission")
    assert conn.closed and len(conn.calls) == 1


def test_invalid_capability_and_port_fail_before_connection():
    with pytest.raises(ValueError):
        LoopbackQueue(True, "synthetic-capability")
    with pytest.raises(ValueError):
        LoopbackQueue(12345, "synthetic\r\ncapability")


def test_oversized_response_closes_without_acceptance():
    conn = Connection(200, b"x" * 65537)
    with patch(
        "hermes_cli.kanban_worker_transport.http.client.HTTPConnection",
        return_value=conn,
    ):
        with pytest.raises(RuntimeError, match="UNCERTAIN"):
            LoopbackQueue(12345, "synthetic-capability").read("mission")
    assert conn.closed
