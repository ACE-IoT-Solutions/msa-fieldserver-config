from __future__ import annotations

import json

import httpx
import pytest
import respx

from msa_fieldserver_config.client import (
    FieldServerAPIError,
    FieldServerAuthError,
    FieldServerClient,
    FieldServerPermissionError,
    FieldServerTransportError,
    normalize_base_url,
)

from .conftest import BASE, PORTS_PAYLOAD, PRODUCT_INFO, UNAUTHENTICATED, failed, ok


def make_client(**kwargs: object) -> FieldServerClient:
    kwargs.setdefault("pacing", 0.0)
    kwargs.setdefault("backoff", 0.0)
    return FieldServerClient("192.0.2.6", "ssi", "secret", **kwargs)  # type: ignore[arg-type]


def mock_login(token: str = "tok-123") -> None:
    respx.post(f"{BASE}/rest/login").mock(
        return_value=httpx.Response(
            200, json={"message": "Logged in ok", "data": {"token": token}, "error": None}
        )
    )


class TestNormalizeBaseUrl:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("192.0.2.6", "http://192.0.2.6"),
            ("192.0.2.6/", "http://192.0.2.6"),
            ("localhost:6080", "http://localhost:6080"),
            ("https://fs.example.com", "https://fs.example.com"),
            ("http://fs.example.com/", "http://fs.example.com"),
        ],
    )
    def test_forms(self, host: str, expected: str) -> None:
        assert normalize_base_url(host) == expected

    def test_scheme_override(self) -> None:
        assert normalize_base_url("192.0.2.6", "https") == "https://192.0.2.6"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            normalize_base_url("   ")


class TestLogin:
    @respx.mock
    def test_success_caches_token(self) -> None:
        mock_login("abc")
        with make_client() as c:
            c.login()
            assert c.authenticated

    @respx.mock
    def test_invalid_credentials(self) -> None:
        respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(
                401,
                json={
                    "message": "Failure logging in",
                    "data": None,
                    "error": {
                        "name": "AccessDenied",
                        "code": 401,
                        "message": "Invalid credentials",
                    },
                },
            )
        )
        with make_client() as c, pytest.raises(FieldServerAuthError, match="invalid credentials"):
            c.login()

    @respx.mock
    def test_password_not_leaked_in_error(self) -> None:
        respx.post(f"{BASE}/rest/login").mock(return_value=httpx.Response(401, json={}))
        with make_client() as c:
            with pytest.raises(FieldServerAuthError) as excinfo:
                c.login()
            assert "secret" not in str(excinfo.value)

    @respx.mock
    def test_missing_token_is_auth_error(self) -> None:
        respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(200, json={"message": "odd", "data": {}, "error": None})
        )
        with make_client() as c, pytest.raises(FieldServerAuthError, match="no token"):
            c.login()


class TestCall:
    @respx.mock
    def test_sends_parameters_envelope_and_bearer(self) -> None:
        mock_login("tok-123")
        route = respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(200, json=ok(PORTS_PAYLOAD))
        )
        with make_client() as c:
            result = c.call("pe", "rpc_db_read_json", db="bacnet_router", path="Ports")

        assert result == PORTS_PAYLOAD
        request = route.calls.last.request
        # Only {"parameters": {...}} is accepted by happner-rest.
        assert json.loads(request.content) == {
            "parameters": {"db": "bacnet_router", "path": "Ports"}
        }
        assert request.headers["Authorization"] == "Bearer tok-123"

    @respx.mock
    def test_logs_in_lazily(self) -> None:
        login_route = respx.post(f"{BASE}/rest/login").mock(
            return_value=httpx.Response(200, json={"data": {"token": "t"}, "error": None})
        )
        respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            return_value=httpx.Response(200, json=ok(PRODUCT_INFO))
        )
        with make_client() as c:
            assert not c.authenticated
            c.get_product_info()
        assert login_route.call_count == 1

    @respx.mock
    def test_call_failed_envelope_raises_api_error_with_code(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(500, json=failed("Missing Required Parameter - db"))
        )
        with make_client() as c, pytest.raises(FieldServerAPIError) as excinfo:
            c.call("pe", "rpc_db_read_json")
        assert excinfo.value.code == -32008

    @respx.mock
    def test_api_error_is_not_retried(self) -> None:
        """A 500 carrying a real envelope is a rejection, not a transport fault."""
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(500, json=failed("Missing Required Parameter - db"))
        )
        with make_client(max_retries=3) as c, pytest.raises(FieldServerAPIError):
            c.call("pe", "rpc_db_read_json")
        assert route.call_count == 1


class TestParameterEnvelopes:
    """The two envelopes are not interchangeable — see call()'s docstring."""

    @respx.mock
    def test_keyword_arguments_send_an_object(self) -> None:
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(200, json=ok(PORTS_PAYLOAD))
        )
        with make_client() as c:
            c.call("pe", "rpc_db_read_json", db="bacnet_router", path="Ports")
        assert json.loads(route.calls.last.request.content) == {
            "parameters": {"db": "bacnet_router", "path": "Ports"}
        }

    @respx.mock
    def test_positional_arguments_send_an_array(self) -> None:
        """A scalar-argument method handed an object replies 'Invalid ...: null'."""
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/fsReadMsgScreen").mock(
            return_value=httpx.Response(200, json=ok("errors"))
        )
        with make_client() as c:
            c.call("pe", "fsReadMsgScreen", "Errors")
        assert json.loads(route.calls.last.request.content) == {"parameters": ["Errors"]}

    @respx.mock
    def test_no_arguments_sends_an_empty_object(self) -> None:
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/isOnline").mock(
            return_value=httpx.Response(200, json=ok(True))
        )
        with make_client() as c:
            assert c.is_online() is True
        assert json.loads(route.calls.last.request.content) == {"parameters": {}}

    def test_mixing_both_forms_is_rejected(self) -> None:
        with make_client() as c, pytest.raises(TypeError, match="not both"):
            c.call("pe", "getConfig", "config.csv", db="x")


class TestCrashGuard:
    """pe/getConfig in object form crashes the device's protocol engine.

    It does not error politely — it takes the engine down, 502s every endpoint
    for ~75s, and on repetition leaves the unit in Recovery Mode needing a
    reboot. The guard makes that shape unreachable rather than merely discouraged.
    """

    @pytest.mark.parametrize("method", ["getConfig", "fsReadMsgScreen"])
    def test_object_envelope_refused_for_scalar_methods(self, method: str) -> None:
        with make_client() as c, pytest.raises(TypeError, match="Recovery Mode"):
            c.call("pe", method, filename="config.csv")

    @pytest.mark.parametrize("method", ["getConfig", "fsReadMsgScreen"])
    def test_empty_call_refused_for_scalar_methods(self, method: str) -> None:
        """No arguments still sends {} — the same dangerous shape."""
        with make_client() as c, pytest.raises(TypeError, match="positional argument"):
            c.call("pe", method)

    def test_guard_never_reaches_the_network(self) -> None:
        """The point is that the request is never issued at all."""
        with respx.mock:
            route = respx.post(f"{BASE}/rest/method/pe/getConfig")
            with make_client() as c, pytest.raises(TypeError):
                c.call("pe", "getConfig", filename="config.csv")
            assert route.call_count == 0

    @respx.mock
    def test_positional_form_still_allowed(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/getConfig").mock(
            return_value=httpx.Response(200, json=ok({"errors": [], "sections": []}))
        )
        with make_client() as c:
            assert c.get_config() == {"errors": [], "sections": []}

    def test_other_methods_unaffected_by_the_guard(self) -> None:
        """Only the listed scalar methods are constrained."""
        with respx.mock:
            respx.post(f"{BASE}/rest/login").mock(
                return_value=httpx.Response(200, json={"data": {"token": "t"}, "error": None})
            )
            respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
                return_value=httpx.Response(200, json=ok(PORTS_PAYLOAD))
            )
            with make_client() as c:
                assert c.call("pe", "rpc_db_read_json", db="bacnet_router") == PORTS_PAYLOAD


class TestPermissions:
    @respx.mock
    def test_access_denied_is_a_permission_error(self) -> None:
        """The ssi account lacks permission for some methods on 8.0.1."""
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/getFirmwareVersion").mock(
            return_value=httpx.Response(500, json=failed("Access denied", code=-32603))
        )
        with make_client() as c, pytest.raises(FieldServerPermissionError, match="Access denied"):
            c.get_firmware_version()

    @respx.mock
    def test_permission_error_is_still_an_api_error(self) -> None:
        """Callers catching the broader type must keep working."""
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/getFirmwareVersion").mock(
            return_value=httpx.Response(500, json=failed("Access denied", code=-32603))
        )
        with make_client() as c, pytest.raises(FieldServerAPIError):
            c.get_firmware_version()


class TestNetworkStatus:
    @respx.mock
    def test_requires_an_interface(self) -> None:
        """Called without one the device replies 'Invalid interface null'."""
        mock_login()
        route = respx.post(f"{BASE}/rest/method/smcNetwork/getNetworkStatus").mock(
            return_value=httpx.Response(200, json=ok({"state": "up"}))
        )
        with make_client() as c:
            c.get_network_status("eth1")
        assert json.loads(route.calls.last.request.content) == {"parameters": {"interface": "eth1"}}


class TestTransportResilience:
    @respx.mock
    def test_retries_plain_text_gateway_error_then_succeeds(self) -> None:
        """The observed failure mode: a tunnel returning plain 'Bad Gateway'."""
        mock_login()
        route = respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json=ok(PRODUCT_INFO)),
            ]
        )
        with make_client(max_retries=3) as c:
            assert c.get_product_info() == PRODUCT_INFO
        assert route.call_count == 3

    @respx.mock
    def test_gives_up_and_raises_transport_error(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            return_value=httpx.Response(502, text="Bad Gateway")
        )
        with (
            make_client(max_retries=2) as c,
            pytest.raises(FieldServerTransportError, match="unreachable"),
        ):
            c.get_product_info()

    @respx.mock
    def test_connection_error_retried(self) -> None:
        mock_login()
        route = respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=ok(PRODUCT_INFO))]
        )
        with make_client(max_retries=3) as c:
            assert c.get_product_info() == PRODUCT_INFO
        assert route.call_count == 2

    @respx.mock
    def test_non_json_200_is_transport_error(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            return_value=httpx.Response(200, text="<html>login</html>")
        )
        with make_client() as c, pytest.raises(FieldServerTransportError, match="non-JSON"):
            c.get_product_info()


class TestTokenExpiry:
    @respx.mock
    def test_bad_origin_triggers_relogin_and_retry(self) -> None:
        login_route = respx.post(f"{BASE}/rest/login").mock(
            side_effect=[
                httpx.Response(200, json={"data": {"token": "stale"}, "error": None}),
                httpx.Response(200, json={"data": {"token": "fresh"}, "error": None}),
            ]
        )
        call_route = respx.post(f"{BASE}/rest/method/smcCore/getProductInfo").mock(
            side_effect=[
                httpx.Response(403, json=UNAUTHENTICATED),
                httpx.Response(200, json=ok(PRODUCT_INFO)),
            ]
        )
        with make_client() as c:
            assert c.get_product_info() == PRODUCT_INFO

        assert login_route.call_count == 2
        assert call_route.calls[-1].request.headers["Authorization"] == "Bearer fresh"


class TestReadWrappers:
    @respx.mock
    def test_read_bacnet_ports_unwraps_data(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(200, json=ok(PORTS_PAYLOAD))
        )
        with make_client() as c:
            ports = c.read_bacnet_ports()
        assert "ETH1 - BACnet IP Wired 1" in ports

    @respx.mock
    def test_read_bacnet_ports_tolerates_unexpected_shape(self) -> None:
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/rpc_db_read_json").mock(
            return_value=httpx.Response(200, json=ok(None))
        )
        with make_client() as c:
            assert c.read_bacnet_ports() == {}

    @respx.mock
    def test_get_config_is_sent_positionally(self) -> None:
        """pe/getConfig takes a bare string, so it needs the array envelope."""
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/getConfig").mock(
            return_value=httpx.Response(200, json=ok("Node,Name\n1,foo\n"))
        )
        with make_client() as c:
            assert c.get_config() == "Node,Name\n1,foo\n"
        assert json.loads(route.calls.last.request.content) == {"parameters": ["config.csv"]}

    @respx.mock
    def test_read_message_screen_is_sent_positionally(self) -> None:
        mock_login()
        route = respx.post(f"{BASE}/rest/method/pe/fsReadMsgScreen").mock(
            return_value=httpx.Response(200, json=ok("SYSTEM -> Running in Recovery Mode."))
        )
        with make_client() as c:
            assert "Recovery Mode" in c.read_message_screen()
        assert json.loads(route.calls.last.request.content) == {"parameters": ["Errors"]}

    @respx.mock
    def test_stored_settings_empty_data_is_success(self) -> None:
        """An empty Data block is a legitimate answer, not a failure."""
        mock_login()
        respx.post(f"{BASE}/rest/method/pe/rpc_bacnet_read_stored_settings").mock(
            return_value=httpx.Response(200, json=ok({"Status": "Success", "Data": {}}))
        )
        with make_client() as c:
            assert c.read_stored_settings() == {"Status": "Success", "Data": {}}
