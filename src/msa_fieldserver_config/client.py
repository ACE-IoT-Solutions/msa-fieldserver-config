"""HTTP client for the MSA / Sierra Monitor FieldServer happner-rest bridge.

The FieldServer web UI is an AngularJS SPA talking to a Happner (happn-3) mesh
over a Primus WebSocket. The firmware also mounts ``happner-rest``, an HTTP
bridge onto the same component exchange, which is what this client uses:

    POST /rest/login                          -> {"data": {"token": ...}}
    POST /rest/method/{component}/{method}    -> {"message": "Call successful", "data": ...}

See the README for protocol notes, including the argument-shape rules that must
be respected — sending a scalar-argument method an object crashes the device.
"""

from __future__ import annotations

import time
from types import TracebackType
from typing import Any, Self

import httpx

__all__ = [
    "DEFAULT_PACING",
    "DEFAULT_SCHEME",
    "DEFAULT_TIMEOUT",
    "FieldServerAPIError",
    "FieldServerAuthError",
    "FieldServerClient",
    "FieldServerError",
    "FieldServerPermissionError",
    "FieldServerTransportError",
    "normalize_base_url",
]

DEFAULT_SCHEME = "http"
DEFAULT_TIMEOUT = 30.0

#: Minimum seconds between requests to one device.
#:
#: This was originally introduced to explain a wave of 502s that turned out to
#: have a different cause entirely — a malformed ``pe/getConfig`` call crashing
#: the protocol engine (see ``_SCALAR_ARGUMENT_METHODS``). There is in fact no
#: evidence these gateways are rate-sensitive: nine sequential reads at 0.15s
#: spacing completed without trouble.
#:
#: It is kept anyway, deliberately, because the risk is asymmetric. Pacing costs
#: seconds on an export; being wrong about an embedded gateway's tolerance costs
#: a site visit to reboot a BACnet router. Lower it via ``pacing=`` once a
#: deployment is known.
DEFAULT_PACING = 0.5

#: Status codes that indicate the path to the device failed, not the call.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: happner-rest's unauthenticated response is a misleading "Bad origin".
_UNAUTHENTICATED_MARKERS = ("origin of call unknown", "bad origin")

#: Marker in a device message screen indicating the unit needs a reboot. In this
#: state the protocol engine's runtime databases (e.g. ``bacnet_router``) are not
#: registered, so reads fail with "Unknown db name" for reasons unrelated to the
#: caller.
RECOVERY_MODE_MARKER = "Running in Recovery Mode"

#: Methods that take a bare scalar and MUST be called positionally.
#:
#: This is a safety guard, not a convenience. Sending ``{"parameters": {...}}``
#: to ``pe/getConfig`` did not merely return an error — it took the protocol
#: engine down. Every observed 502 episode on the test unit began with exactly
#: that call, including one run where nine other reads had just succeeded, and
#: the device eventually came back up in Recovery Mode and needed a reboot.
#:
#: Passing the right shape is fine: the same method returns 200 when called
#: positionally. So the shape is enforced here rather than left to the caller.
_SCALAR_ARGUMENT_METHODS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pe", "getConfig"),
        ("pe", "fsReadMsgScreen"),
    }
)


class FieldServerError(Exception):
    """Base class for all FieldServer client errors."""


class FieldServerAuthError(FieldServerError):
    """Login was rejected, or the session could not be re-established."""


class FieldServerTransportError(FieldServerError):
    """The device could not be reached, or replied with a non-API response.

    Raised for connection failures, timeouts, gateway errors and non-JSON
    bodies. These are worth retrying; :class:`FieldServerAPIError` is not.
    """


class FieldServerAPIError(FieldServerError):
    """The device received the call and rejected it.

    Carries happn's error ``code`` (e.g. ``-32008`` for a missing parameter).
    """

    def __init__(
        self, message: str, *, code: int | None = None, component: str = "", method: str = ""
    ) -> None:
        super().__init__(message)
        self.code = code
        self.component = component
        self.method = method


class FieldServerPermissionError(FieldServerAPIError):
    """The account is not authorised for this component method.

    Distinct from :class:`FieldServerAPIError` because the remedy is different:
    the firmware supports the call, the logged-in user simply lacks the group
    permission for it. Observed on 8.0.1 with the ``ssi`` service account for
    ``pe/getFirmwareVersion`` and ``smcNetwork/getAllNetworkSettings``.
    """


def normalize_base_url(host: str, scheme: str = DEFAULT_SCHEME) -> str:
    """Turn a user-supplied host into a base URL.

    Accepts a bare IP/hostname (``192.0.2.6``), a ``host:port`` pair
    (``localhost:6080``, as produced by an SSH port-forward), or a full URL.
    """
    host = host.strip().rstrip("/")
    if not host:
        raise ValueError("empty host")
    if host.startswith(("http://", "https://")):
        return host
    return f"{scheme}://{host}"


class FieldServerClient:
    """Read-only client for a single FieldServer.

    Only read methods are wrapped. The device exposes destructive neighbours on
    the same components (``pe.postConfig``, ``pe.restart``,
    ``smcNetwork.setNetworkSettings``), so writes are deliberately absent from
    this API — reaching them requires an explicit :meth:`call`.

    Usage::

        with FieldServerClient("192.0.2.6", "ssi", secret) as fs:
            info = fs.get_product_info()
            ports = fs.read_bacnet_ports()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        scheme: str = DEFAULT_SCHEME,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
        max_retries: int = 3,
        backoff: float = 1.0,
        pacing: float = DEFAULT_PACING,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = normalize_base_url(host, scheme)
        self.host = host
        self._username = username
        self._password = password
        self._max_retries = max(1, max_retries)
        self._backoff = backoff
        self._pacing = pacing
        self._token: str | None = None
        self._last_request_at = 0.0
        self._owns_client = client is None
        self._http = client or httpx.Client(
            base_url=self.base_url, timeout=timeout, verify=verify_tls
        )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #

    def _sleep_for_pacing(self) -> None:
        """Space out requests.

        The device (or an intervening tunnel) starts returning gateway errors
        under a rapid burst of calls, so requests are paced by default.
        """
        if self._pacing <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._pacing:
            time.sleep(self._pacing - elapsed)

    def _post(self, path: str, payload: dict[str, Any], *, token: str | None) -> httpx.Response:
        """POST with retry on transport-level failure only."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        last_error: str = "no attempt made"
        for attempt in range(self._max_retries):
            if attempt:
                time.sleep(self._backoff * (2 ** (attempt - 1)))
            self._sleep_for_pacing()
            try:
                response = self._http.post(path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            finally:
                self._last_request_at = time.monotonic()

            # A retryable status is only transport-level if the body isn't a
            # proper API envelope — happner returns 500 for legitimate call
            # failures, which must surface immediately rather than be retried.
            if response.status_code in _RETRYABLE_STATUS and not _looks_like_envelope(response):
                last_error = f"HTTP {response.status_code}: {response.text[:200]!r}"
                continue
            return response

        raise FieldServerTransportError(
            f"{self.base_url}{path} unreachable after {self._max_retries} attempts ({last_error})"
        )

    # ------------------------------------------------------------------ #
    # authentication
    # ------------------------------------------------------------------ #

    def login(self) -> None:
        """Authenticate and cache the bearer token.

        No cookie is issued to non-browser clients, so the token is carried
        explicitly on every subsequent call.
        """
        response = self._post(
            "/rest/login",
            {"username": self._username, "password": self._password},
            token=None,
        )
        if response.status_code == 401:
            raise FieldServerAuthError(
                f"{self.base_url}: invalid credentials for user {self._username!r}"
            )

        body = _parse_json(response, context=f"{self.base_url}/rest/login")
        token = body.get("data", {}).get("token") if isinstance(body.get("data"), dict) else None
        if not isinstance(token, str) or not token:
            raise FieldServerAuthError(
                f"{self.base_url}: login returned no token (message={body.get('message')!r})"
            )
        self._token = token

    # ------------------------------------------------------------------ #
    # the single call chokepoint
    # ------------------------------------------------------------------ #

    def call(self, component: str, method: str, *args: Any, **parameters: Any) -> Any:
        """Invoke a mesh component method and return its unwrapped ``data``.

        happner-rest hands the ``parameters`` value to the method as its
        argument list, and the shape matters — the two forms are not
        interchangeable:

        - **Keyword arguments** send ``{"parameters": {...}}``, which the method
          receives as a single options object. Correct for the ``rpc_*`` family::

              call("pe", "rpc_db_read_json", db="bacnet_router", path="Ports")

        - **Positional arguments** send ``{"parameters": [...]}``. Required for
          methods the UI calls with a bare scalar, which would otherwise be
          handed an object and reject it (``Invalid selectString: null``)::

              call("pe", "fsReadMsgScreen", "Errors")

        ``{"args": [...]}`` and a bare argument object are both rejected.
        """
        if args and parameters:
            raise TypeError(
                "pass either positional or keyword parameters to call(), not both — "
                "they map to different happner-rest envelopes"
            )

        # Refuse to put a known scalar-argument method in the shape that crashes
        # the protocol engine. Cheap to check; the failure it prevents costs a
        # device reboot.
        if (component, method) in _SCALAR_ARGUMENT_METHODS and not args:
            raise TypeError(
                f"{component}/{method} takes a positional argument, e.g. "
                f'call("{component}", "{method}", "<value>"). Sending it an object '
                "has been observed to crash the device's protocol engine and force it "
                "into Recovery Mode, requiring a reboot."
            )

        if not self.authenticated:
            self.login()

        path = f"/rest/method/{component}/{method}"
        payload: dict[str, Any] = {"parameters": list(args) if args else parameters}
        response = self._post(path, payload, token=self._token)

        # An expired token presents as "Bad origin"; re-login once and retry.
        if response.status_code == 403 and _is_unauthenticated(response):
            self._token = None
            self.login()
            response = self._post(path, payload, token=self._token)

        body = _parse_json(response, context=f"{self.base_url}{path}")
        error = body.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            if response.status_code in (401, 403):
                raise FieldServerAuthError(f"{component}/{method}: {message}")
            # "Access denied" means the session is fine but the account lacks
            # the group permission — a per-method problem, not a session one.
            if isinstance(message, str) and "access denied" in message.lower():
                raise FieldServerPermissionError(
                    f"{component}/{method}: {message}",
                    code=code if isinstance(code, int) else None,
                    component=component,
                    method=method,
                )
            raise FieldServerAPIError(
                f"{component}/{method}: {message}",
                code=code if isinstance(code, int) else None,
                component=component,
                method=method,
            )
        if response.status_code >= 400:
            raise FieldServerAPIError(
                f"{component}/{method}: HTTP {response.status_code}",
                component=component,
                method=method,
            )
        return body.get("data")

    # ------------------------------------------------------------------ #
    # read wrappers
    # ------------------------------------------------------------------ #

    def get_product_info(self) -> Any:
        """Device identity: product name, firmware version, customer."""
        return self.call("smcCore", "getProductInfo")

    def get_firmware_version(self) -> Any:
        return self.call("pe", "getFirmwareVersion")

    def get_system_status(self) -> Any:
        return self.call("systemStatus", "getSystemStatus")

    def get_all_network_settings(self) -> Any:
        return self.call("smcNetwork", "getAllNetworkSettings")

    def get_network_status(self, interface: str) -> Any:
        """Status of one named interface.

        The interface is required: called without one the device replies
        ``Invalid interface null``.
        """
        return self.call("smcNetwork", "getNetworkStatus", interface=interface)

    def get_network_snapshot(self) -> Any:
        """Whole-network snapshot, needing no interface argument."""
        return self.call("smcNetwork", "getSnapshot")

    def get_configured_interfaces(self) -> Any:
        return self.call("smcNetwork", "getAllInterfacesConfigured")

    def get_all_stats(self) -> Any:
        return self.call("stats", "getAllStats")

    def read_stored_settings(self) -> Any:
        """BACnet stored settings.

        Returns ``{"Status": "Success", "Data": {}}`` on units with nothing
        stored — an empty result here is success, not failure.
        """
        return self.call("pe", "rpc_bacnet_read_stored_settings")

    def read_router_db(self, path: str | None = None) -> Any:
        """Read the ``bacnet_router`` JSON database, optionally one sub-path.

        With ``path="Ports"`` this is the richest single call on the device: it
        returns port configuration, message/error counters and the routing
        table together.
        """
        if path is None:
            return self.call("pe", "rpc_db_read_json", db="bacnet_router")
        return self.call("pe", "rpc_db_read_json", db="bacnet_router", path=path)

    def read_bacnet_ports(self) -> dict[str, Any]:
        """Port definitions keyed by port name, e.g. ``"ETH1 - BACnet IP Wired 1"``."""
        result = self.read_router_db("Ports")
        if isinstance(result, dict):
            data = result.get("Data")
            if isinstance(data, dict):
                return data
        return {}

    def get_config(self, filename: str = "config.csv") -> Any:
        """Fetch a configuration file, e.g. ``config.csv``.

        Passed positionally, matching the UI's ``pe.getConfig("config.csv")``.
        The object envelope makes this method fail, so the array form is
        required — see :meth:`call`.

        Returns the file already parsed into ``{"errors": [], "sections": [...]}``
        rather than raw CSV. Because it reads stored configuration rather than
        runtime state, it still works on a device in Recovery Mode.
        """
        return self.call("pe", "getConfig", filename)

    def read_message_screen(self, screen: str = "Errors") -> Any:
        """Read a device message screen — ``"Errors"`` carries system faults.

        Worth calling when a device behaves oddly: a unit in Recovery Mode
        reports it here, which explains otherwise baffling failures such as the
        ``bacnet_router`` database being an unknown name.
        """
        return self.call("pe", "fsReadMsgScreen", screen)

    def is_online(self) -> Any:
        """Whether the protocol engine reports itself online."""
        return self.call("pe", "isOnline")


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _parse_json(response: httpx.Response, *, context: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise FieldServerTransportError(
            f"{context}: HTTP {response.status_code} with non-JSON body ({response.text[:200]!r})"
        ) from exc
    if not isinstance(body, dict):
        raise FieldServerTransportError(
            f"{context}: unexpected JSON payload type {type(body).__name__}"
        )
    return body


def _looks_like_envelope(response: httpx.Response) -> bool:
    """True if the body is a happner envelope rather than a proxy error page."""
    if "json" not in response.headers.get("content-type", "").lower():
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and ("message" in body or "error" in body)


def _is_unauthenticated(response: httpx.Response) -> bool:
    text = response.text.lower()
    return any(marker in text for marker in _UNAUTHENTICATED_MARKERS)
