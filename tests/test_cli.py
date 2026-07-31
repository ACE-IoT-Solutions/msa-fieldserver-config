from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from msa_fieldserver_config.cli import app

from .conftest import BASE, ok
from .test_collect_export import _mock_device

runner = CliRunner()

#: Pacing is a live-device protection; it only slows the mocked suite down.
NO_PACING = ["--pacing", "0"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real .env must not leak into CLI tests."""
    for var in (
        "FIELDSERVER_USERNAME",
        "FIELDSERVER_PASSWORD",
        "FIELDSERVER_HOSTS",
        "FIELDSERVER_SCHEME",
        "FIELDSERVER_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


class TestVersion:
    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert result.stdout.strip()


class TestArgumentValidation:
    def test_no_hosts_exits_2(self) -> None:
        result = runner.invoke(
            app,
            [
                "export",
                "-u",
                "ssi",
                "-p",
                "secret",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 2
        assert "No hosts" in result.output

    def test_missing_credentials_exits_2(self) -> None:
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 2
        assert "No username" in result.output

    def test_missing_password_exits_2(self) -> None:
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                "-u",
                "ssi",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 2
        assert "No password" in result.output

    def test_missing_hosts_file_exits_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "export",
                "-u",
                "ssi",
                "-p",
                "x",
                "-f",
                str(tmp_path / "nope.txt"),
                *NO_PACING,
            ],
        )
        assert result.exit_code == 2
        assert "No such hosts file" in result.output

    def test_hosts_come_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FIELDSERVER_HOSTS is the fallback when no hosts are given."""
        monkeypatch.setenv("FIELDSERVER_HOSTS", "")
        result = runner.invoke(
            app,
            [
                "export",
                "-u",
                "ssi",
                "-p",
                "x",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 2


class TestExportCommand:
    @respx.mock
    def test_writes_config_and_exits_0(self, tmp_path: Path) -> None:
        _mock_device()
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "secret",
                "-o",
                str(tmp_path),
                *NO_PACING,
            ],
        )
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "192.0.2.6.json").read_text())
        assert payload["product"]["product_name"] == "BACnet Router"

    @respx.mock
    def test_reads_hosts_file(self, tmp_path: Path) -> None:
        _mock_device()
        hosts_file = tmp_path / "hosts.txt"
        hosts_file.write_text("# fleet\n192.0.2.6\n")
        result = runner.invoke(
            app,
            [
                "export",
                "-f",
                str(hosts_file),
                "-u",
                "ssi",
                "-p",
                "secret",
                "-o",
                str(tmp_path),
                *NO_PACING,
            ],
        )
        assert result.exit_code == 0
        assert (tmp_path / "192.0.2.6.json").exists()

    @respx.mock
    def test_credentials_from_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_device()
        monkeypatch.setenv("FIELDSERVER_USERNAME", "ssi")
        monkeypatch.setenv("FIELDSERVER_PASSWORD", "secret")
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                "-o",
                str(tmp_path),
                *NO_PACING,
            ],
        )
        assert result.exit_code == 0

    @respx.mock
    def test_failed_host_exits_1(self, tmp_path: Path) -> None:
        respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(401, json={"message": "Failure logging in"})
        )
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "bad",
                "-o",
                str(tmp_path),
                "--timeout",
                "1",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 1
        assert "failed" in result.output.lower()

    @respx.mock
    def test_password_never_printed(self, tmp_path: Path) -> None:
        respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(401, json={"message": "Failure logging in"})
        )
        result = runner.invoke(
            app,
            [
                "export",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "hunter2",
                "-o",
                str(tmp_path),
                "--timeout",
                "1",
                *NO_PACING,
            ],
        )
        assert "hunter2" not in result.output


class TestStatsCommand:
    @respx.mock
    def test_writes_stats_file(self, tmp_path: Path) -> None:
        _mock_device()
        result = runner.invoke(
            app,
            [
                "stats",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "secret",
                "-o",
                str(tmp_path),
                *NO_PACING,
            ],
        )
        assert result.exit_code == 0
        payload = json.loads((tmp_path / "192.0.2.6-stats.json").read_text())
        ports = payload["ports"]["ETH1 - BACnet IP Wired 1"]
        assert ports["statistics"]["info"]["Messages Sent"] == 4698
        assert ports["routing_table"][0]["dnet"] == 15097


class TestProbeCommand:
    @respx.mock
    def test_all_reads_ok_exits_0(self) -> None:
        _mock_device()
        result = runner.invoke(
            app,
            [
                "probe",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "secret",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 0
        assert "Authenticated" in result.output

    @respx.mock
    def test_unsupported_read_exits_1(self) -> None:
        """probe's job is to surface which reads this firmware lacks."""
        _mock_device(failing={"pe/getConfig"})
        result = runner.invoke(
            app,
            [
                "probe",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "secret",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 1
        assert "pe/getConfig" in result.output

    @respx.mock
    def test_recovery_mode_is_called_out(self) -> None:
        """Recovery Mode explains failures that otherwise look like our bug."""
        _mock_device()
        respx.post(f"{BASE}/rest/method/pe/fsReadMsgScreen").mock(
            return_value=httpx.Response(
                200, json=ok("T+00:00:00 - SYSTEM -> Running in Recovery Mode. Reboot to reset")
            )
        )
        result = runner.invoke(app, ["probe", "192.0.2.6", "-u", "ssi", "-p", "secret", *NO_PACING])
        assert result.exit_code == 1
        assert "Recovery Mode" in result.output

    @respx.mock
    def test_healthy_device_not_flagged_as_recovery(self) -> None:
        _mock_device()
        respx.post(f"{BASE}/rest/method/pe/fsReadMsgScreen").mock(
            return_value=httpx.Response(200, json=ok("T+00:00:00 - SYSTEM -> Ready"))
        )
        result = runner.invoke(app, ["probe", "192.0.2.6", "-u", "ssi", "-p", "secret", *NO_PACING])
        assert result.exit_code == 0
        assert "Recovery Mode" not in result.output

    @respx.mock
    def test_bad_credentials_exits_1(self) -> None:
        respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(401, json={"message": "Failure logging in"})
        )
        result = runner.invoke(
            app,
            [
                "probe",
                "192.0.2.6",
                "-u",
                "ssi",
                "-p",
                "bad",
                "--timeout",
                "1",
                *NO_PACING,
            ],
        )
        assert result.exit_code == 1
