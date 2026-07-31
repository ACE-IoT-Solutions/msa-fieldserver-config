"""Shared fixtures. Payloads are verbatim captures from a live BACnet Router 8.0.1."""

from __future__ import annotations

import pytest

BASE = "http://192.0.2.6"

PRODUCT_INFO = {
    "productName": "BACnet Router",
    "productVersion": "8.0.1",
    "customerName": "MSA Safety",
    "skin": "msa",
}

PORTS_PAYLOAD = {
    "Data": {
        "ETH1 - BACnet IP Wired 1": {
            "Network Number": 15051,
            "BACnet_IP Conn Instance": 0,
            "Stats": {
                "Info": {"Messages Sent": 4698, "Messages Received": 22631},
                "Error": {
                    "Total Errors": 9,
                    "BACnet NL RX Reject Msg": 6,
                    "Error: Property - Unknown Property": 3,
                },
            },
            "Routing Table": [
                {"DNET": 15097, "MAC Address": "192.0.2.9:47808", "State": "Available"},
                {"DNET": 2701, "MAC Address": "192.0.2.27:47808", "State": "Available"},
            ],
        }
    }
}


# pe/getConfig returns config.csv already parsed into ordered sections. Section
# names repeat (one "Connections" block per connection), hence a list of dicts.
CONFIG_FILE = {
    "errors": [],
    "sections": [
        {"Bridge": [{"Title": "FieldServer BACnet Router"}]},
        {
            "Connections": [
                {
                    "Adapter": "N1",
                    "Connection_Name": "BACnet IP Wired 1",
                    "Protocol": "Bacnet_IP",
                    "Router_Network_Number": "15051",
                    "IP_Port": "47808",
                    "Skip_Creation": "no",
                }
            ]
        },
        {
            "Connections": [
                {
                    "Adapter": "N1",
                    "Connection_Name": "BACnet IP Wired 2",
                    "Protocol": "Bacnet_IP",
                    "Router_Network_Number": "2",
                    "IP_Port": "47809",
                    "Skip_Creation": "yes",
                }
            ]
        },
    ],
}


def ok(data: object) -> dict[str, object]:
    """A successful happner-rest envelope."""
    return {"message": "Call successful", "data": data, "error": None}


def failed(message: str, code: int = -32008) -> dict[str, object]:
    """A 'the device rejected this call' envelope."""
    return {"message": "Call failed", "data": None, "error": {"message": message, "code": code}}


UNAUTHENTICATED = {
    "message": "Bad origin",
    "data": None,
    "error": {"message": "origin of call unknown"},
}


@pytest.fixture
def credentials() -> tuple[str, str]:
    return ("ssi", "not-a-real-password")
