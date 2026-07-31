from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from msa_fieldserver_config.client import FieldServerClient
from msa_fieldserver_config.collect import collect_config, collect_stats, split_port
from msa_fieldserver_config.export import export_host, export_hosts, read_hosts_file, safe_filename

from .conftest import BASE, CONFIG_FILE, PORTS_PAYLOAD, PRODUCT_INFO, failed, ok

PORT = PORTS_PAYLOAD["Data"]["ETH1 - BACnet IP Wired 1"]  # type: ignore[index]

#: Disable pacing and retry backoff so the suite doesn't sleep through them.
FAST = {"pacing": 0.0, "backoff": 0.0}


class TestSplitPort:
    def test_config_excludes_volatile_keys(self) -> None:
        config, _ = split_port(PORT)  # type: ignore[arg-type]
        assert config == {"Network Number": 15051, "BACnet_IP Conn Instance": 0}
        assert "Stats" not in config
        assert "Routing Table" not in config

    def test_statistics_captured(self) -> None:
        _, stats = split_port(PORT)  # type: ignore[arg-type]
        assert stats.statistics.info["Messages Sent"] == 4698
        assert stats.statistics.error["Total Errors"] == 9

    def test_routing_table_parsed(self) -> None:
        _, stats = split_port(PORT)  # type: ignore[arg-type]
        assert len(stats.routing_table) == 2
        assert stats.routing_table[0].dnet == 15097
        assert stats.routing_table[0].mac_address == "192.0.2.9:47808"
        assert stats.routing_table[0].state == "Available"

    def test_missing_sections_tolerated(self) -> None:
        config, stats = split_port({"Network Number": 7})
        assert config == {"Network Number": 7}
        assert stats.routing_table == []
        assert stats.statistics.info == {}

    def test_unknown_keys_preserved(self) -> None:
        config, _ = split_port({"Network Number": 1, "Future Field": "keep me"})
        assert config["Future Field"] == "keep me"


def _mock_device(*, failing: set[str] | None = None) -> None:
    """Stub a full device. Names in `failing` return a 'Call failed' envelope."""
    failing = failing or set()
    respx.post(f"{BASE}/rest/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "t"}, "error": None})
    )
    responses = {
        "smcCore/getProductInfo": PRODUCT_INFO,
        "pe/getFirmwareVersion": {"version": "8.0.1"},
        "smcNetwork/getAllNetworkSettings": {"eth1": {"dhcp": False}},
        "smcNetwork/getSnapshot": {"eth1": "up"},
        "systemStatus/getSystemStatus": {"state": "online"},
        "stats/getAllStats": {"uptime": 1234},
        "pe/rpc_bacnet_read_stored_settings": {"Status": "Success", "Data": {}},
        "pe/getConfig": CONFIG_FILE,
        "pe/fsReadMsgScreen": "T+00:00:00 - SYSTEM -> Ready",
        "pe/isOnline": True,
    }
    for name, payload in responses.items():
        route = respx.post(f"{BASE}/rest/method/{name}")
        if name in failing:
            route.mock(return_value=httpx.Response(500, json=failed("not supported", code=-32601)))
        else:
            route.mock(return_value=httpx.Response(200, json=ok(payload)))

    # rpc_db_read_json serves Ports plus opportunistic sections.
    def db_router(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content).get("parameters", {})
        if params.get("path") == "Ports":
            if "ports" in failing:
                return httpx.Response(500, json=failed("nope", code=-32601))
            return httpx.Response(200, json=ok(PORTS_PAYLOAD))
        return httpx.Response(500, json=failed("no such path", code=-32601))

    respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(side_effect=db_router)


def _client(**kwargs: object) -> FieldServerClient:
    kwargs.setdefault("pacing", 0.0)
    kwargs.setdefault("backoff", 0.0)
    return FieldServerClient("192.0.2.6", "ssi", "secret", **kwargs)  # type: ignore[arg-type]


class TestCollectConfig:
    @respx.mock
    def test_full_collection(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_config(c)

        assert doc.host == "192.0.2.6"
        assert doc.product is not None
        assert doc.product.product_version == "8.0.1"
        assert doc.bacnet_ports["ETH1 - BACnet IP Wired 1"].network_number == 15051
        assert doc.complete
        assert doc.errors == []

    @respx.mock
    def test_config_document_excludes_volatile_data(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_config(c)
        dumped = json.dumps(doc.model_dump(mode="json"))
        assert "Messages Sent" not in dumped
        assert "Routing Table" not in dumped

    @respx.mock
    def test_partial_collection_records_errors_and_continues(self) -> None:
        _mock_device(failing={"pe/getFirmwareVersion", "stats/getAllStats"})
        with _client() as c:
            doc = collect_config(c)

        assert doc.firmware_version is None
        assert not doc.complete
        assert [e.source for e in doc.errors] == ["pe/getFirmwareVersion"]
        # everything else still collected
        assert doc.product is not None
        assert doc.bacnet_ports


class TestConfigFile:
    @respx.mock
    def test_parsed_into_sections(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_config(c)

        assert doc.config_file is not None
        assert doc.config_file.errors == []
        assert len(doc.config_file.sections) == 3

    @respx.mock
    def test_section_helper_flattens_repeated_names(self) -> None:
        """'Connections' appears once per connection, so rows must accumulate."""
        _mock_device()
        with _client() as c:
            doc = collect_config(c)

        assert doc.config_file is not None
        connections = doc.config_file.section("Connections")
        assert [c["Connection_Name"] for c in connections] == [
            "BACnet IP Wired 1",
            "BACnet IP Wired 2",
        ]
        assert doc.config_file.section("Bridge")[0]["Title"] == "FieldServer BACnet Router"

    @respx.mock
    def test_unknown_section_is_empty_not_an_error(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_config(c)
        assert doc.config_file is not None
        assert doc.config_file.section("Nope") == []

    @respx.mock
    def test_survives_when_runtime_db_is_unavailable(self) -> None:
        """A device in Recovery Mode has no bacnet_router db, but config.csv reads fine."""
        _mock_device(failing={"ports"})
        with _client() as c:
            doc = collect_config(c)

        assert doc.bacnet_ports == {}
        assert doc.config_file is not None
        assert doc.config_file.section("Connections")


class TestReadOrdering:
    @respx.mock
    def test_ports_are_read_before_getconfig(self) -> None:
        """Ordering is load-bearing, not cosmetic.

        pe/getConfig reliably 502s on 8.0.1 and its retries knock the bridge
        out for whatever runs next. The Ports read — the single most valuable
        call on the device — must not be sitting behind it.
        """
        _mock_device()
        order: list[str] = []

        def record(name: str, payload: object):
            def handler(request: httpx.Request) -> httpx.Response:
                order.append(name)
                return httpx.Response(200, json=ok(payload))

            return handler

        respx.post(f"{BASE}/rest/method/pe/getConfig").mock(side_effect=record("getConfig", "csv"))
        respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            side_effect=record("ports", PORTS_PAYLOAD)
        )

        with _client() as c:
            collect_config(c)

        assert "ports" in order and "getConfig" in order
        assert order.index("ports") < order.index("getConfig")


class TestCollectStats:
    @respx.mock
    def test_full_collection(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_stats(c)

        entry = doc.ports["ETH1 - BACnet IP Wired 1"]
        assert entry.statistics.info["Messages Received"] == 22631
        assert entry.routing_table[1].dnet == 2701
        assert doc.system_status == {"state": "online"}
        assert doc.complete

    @respx.mock
    def test_stats_document_excludes_config_only_fields(self) -> None:
        _mock_device()
        with _client() as c:
            doc = collect_stats(c)
        assert not hasattr(doc, "network_settings")


class TestSafeFilename:
    @pytest.mark.parametrize(
        ("host", "kind", "expected"),
        [
            ("192.0.2.6", "config", "192.0.2.6.json"),
            ("192.0.2.6", "stats", "192.0.2.6-stats.json"),
            ("localhost:6080", "config", "localhost_6080.json"),
            ("https://fs.example.com/", "config", "fs.example.com.json"),
            ("../../etc/passwd", "config", ".._.._etc_passwd.json"),
        ],
    )
    def test_names(self, host: str, kind: str, expected: str) -> None:
        assert safe_filename(host, kind) == expected  # type: ignore[arg-type]

    def test_never_escapes_output_directory(self) -> None:
        name = safe_filename("../../etc/passwd", "config")
        assert "/" not in name


class TestReadHostsFile:
    def test_skips_blanks_and_comments(self, tmp_path: Path) -> None:
        f = tmp_path / "hosts.txt"
        f.write_text("10.0.0.1\n\n# a comment\n10.0.0.2  # trailing\n   \n")
        assert read_hosts_file(f) == ["10.0.0.1", "10.0.0.2"]


class TestExport:
    @respx.mock
    def test_writes_config_file(self, tmp_path: Path) -> None:
        _mock_device()
        result = export_host("192.0.2.6", "ssi", "secret", tmp_path, **FAST)

        assert result.ok
        assert result.path == tmp_path / "192.0.2.6.json"
        assert result.path is not None
        payload = json.loads(result.path.read_text())
        assert payload["product"]["product_version"] == "8.0.1"

    @respx.mock
    def test_writes_stats_file(self, tmp_path: Path) -> None:
        _mock_device()
        result = export_host("192.0.2.6", "ssi", "secret", tmp_path, **FAST, kind="stats")

        assert result.path == tmp_path / "192.0.2.6-stats.json"
        assert result.path is not None
        payload = json.loads(result.path.read_text())
        assert (
            payload["ports"]["ETH1 - BACnet IP Wired 1"]["statistics"]["info"]["Messages Sent"]
            == 4698
        )

    @respx.mock
    def test_partial_export_still_writes_and_reports(self, tmp_path: Path) -> None:
        _mock_device(failing={"pe/getConfig"})
        result = export_host("192.0.2.6", "ssi", "secret", tmp_path, **FAST)

        assert result.ok
        assert result.partial_errors == 1

    @respx.mock
    def test_unreachable_host_reported_not_raised(self, tmp_path: Path) -> None:
        respx.post(f"{BASE}/rest/login").mock(return_value=httpx.Response(502, text="Bad Gateway"))
        result = export_host("192.0.2.6", "ssi", "secret", tmp_path, timeout=1.0, **FAST)

        assert not result.ok
        assert result.error is not None
        assert result.path is None

    @respx.mock
    def test_one_bad_host_does_not_abort_the_run(self, tmp_path: Path) -> None:
        _mock_device()
        respx.post("http://192.0.2.99/rest/login").mock(
            return_value=httpx.Response(401, json={"message": "Failure logging in"})
        )
        results = export_hosts(
            ["192.0.2.6", "192.0.2.99"], "ssi", "secret", tmp_path, workers=1, **FAST
        )

        assert {r.host: r.ok for r in results} == {"192.0.2.6": True, "192.0.2.99": False}
        assert (tmp_path / "192.0.2.6.json").exists()

    @respx.mock
    def test_duplicate_hosts_collected_once(self, tmp_path: Path) -> None:
        _mock_device()
        results = export_hosts(
            ["192.0.2.6", "192.0.2.6", " 192.0.2.6 "],
            "ssi",
            "secret",
            tmp_path,
            workers=1,
            **FAST,
        )
        assert len(results) == 1

    def test_empty_host_list(self, tmp_path: Path) -> None:
        assert export_hosts([], "ssi", "secret", tmp_path) == []
