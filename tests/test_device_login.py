"""`spooling login`: the key is made locally, only its hash is sent, and every failure is explained."""
import hashlib

import httpx
import pytest

from spooling import device_login
from spooling.device_login import LoginError, run_device_login


class FakeHttp:
    """Stands in for httpx: records requests and replays a scripted server."""
    HTTPError = httpx.HTTPError

    def __init__(self, polls, code_status=200):
        self.polls, self.code_status, self.sent, self.poll_calls = list(polls), code_status, None, 0

    def post(self, url, json=None, timeout=None):
        if url.endswith("/device/code"):
            self.sent = json
            body = {"device_code": "dc", "user_code": "BCDF-GHJK", "verification_uri_complete": "https://app.spooling.ai/device?code=BCDF-GHJK",
                    "expires_in": 600, "interval": 5}
            return httpx.Response(self.code_status, json=body, request=httpx.Request("POST", url))
        self.poll_calls += 1
        item = self.polls.pop(0) if self.polls else {"status": "pending"}
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item, request=httpx.Request("POST", url))


class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, s): self.t += s


def run(polls, **kw):
    clock, http = Clock(), FakeHttp(polls, **kw)
    return http, lambda: run_device_login("https://api.spooling.ai/", open_browser=False, sleep=clock.sleep, now=clock.now, http=http)


def test_approved_returns_the_locally_generated_key_and_only_its_hash_was_sent():
    http, go = run([{"status": "pending"}, {"status": "approved", "workspace": {"id": "w", "name": "Acme"}}])
    key, ws = go()
    assert key.startswith("sk_") and len(key) > 40 and ws == {"id": "w", "name": "Acme"}
    assert http.sent["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
    assert key not in str(http.sent) and http.sent["key_prefix"] == key[:12]
    assert http.poll_calls == 2


def test_each_login_makes_a_fresh_key():
    assert run([{"status": "approved", "workspace": {}}])[1]()[0] != run([{"status": "approved", "workspace": {}}])[1]()[0]


@pytest.mark.parametrize("status,needle", [("denied", "denied"), ("expired", "expired")])
def test_denied_and_expired_are_explained(status, needle):
    with pytest.raises(LoginError, match=needle):
        run([{"status": status}])[1]()


def test_network_blips_while_polling_do_not_abort_the_login():
    _, go = run([httpx.ConnectError("boom"), {"status": "approved", "workspace": {"name": "Acme"}}])
    assert go()[1]["name"] == "Acme"


def test_slow_down_widens_the_interval():
    clock = Clock()
    http = FakeHttp([{"status": "slow_down", "interval": 10}, {"status": "approved", "workspace": {}}])
    run_device_login("https://x", open_browser=False, sleep=clock.sleep, now=clock.now, http=http)
    assert clock.t == 5 + 10


def test_gives_up_when_the_code_window_closes():
    clock = Clock()
    http = FakeHttp([])                                     # pending forever
    with pytest.raises(LoginError, match="Timed out"):
        run_device_login("https://x", open_browser=False, sleep=clock.sleep, now=clock.now, http=http)
    assert clock.t >= 600


def test_rate_limited_and_server_errors_are_explained():
    with pytest.raises(LoginError, match="Too many"):
        run([], code_status=429)[1]()
    with pytest.raises(LoginError, match="refused"):
        run([], code_status=500)[1]()


def test_unreachable_server_is_explained():
    class Down:
        HTTPError = httpx.HTTPError
        def post(self, *a, **k): raise httpx.ConnectError("no route")
    with pytest.raises(LoginError, match="Could not reach"):
        run_device_login("https://nowhere", open_browser=False, http=Down())


def test_the_approval_url_is_printed_even_if_the_browser_cannot_open(monkeypatch):
    printed = []
    class Out:
        def print(self, *a, **k): printed.append(" ".join(str(x) for x in a))
    monkeypatch.setattr(device_login.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("no display")))
    clock, http = Clock(), FakeHttp([{"status": "approved", "workspace": {}}])
    run_device_login("https://x", open_browser=True, sleep=clock.sleep, now=clock.now, http=http, out=Out())
    assert any("BCDF-GHJK" in p for p in printed) and any("/device?code=BCDF-GHJK" in p for p in printed)


# --- wiring: `spooling login` and `spooling cloud login` ----------------------------------------------------------

def _cli(monkeypatch, tmp_path, result=("sk_made_locally", {"id": "w", "name": "Acme"})):
    from spooling import cloud
    monkeypatch.setattr(cloud, "CONFIG_PATH", tmp_path / "cloud.json")
    monkeypatch.setattr(cloud.httpx, "get", lambda *a, **k: httpx.Response(200, json={"sessions": 3}, request=httpx.Request("GET", a[0])))
    calls = []
    import spooling.device_login as dl
    monkeypatch.setattr(dl, "run_device_login", lambda base, open_browser=True, **k: calls.append((base, open_browser)) or result)
    return cloud, calls


def test_top_level_login_saves_the_key_with_private_permissions(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from spooling.cli import cli
    cloud, calls = _cli(monkeypatch, tmp_path)
    r = CliRunner().invoke(cli, ["login"])
    assert r.exit_code == 0, r.output
    assert calls == [("https://api.spooling.ai", True)]
    assert cloud._load_config()["api_key"] == "sk_made_locally"
    assert oct((tmp_path / "cloud.json").stat().st_mode & 0o777) == "0o600"
    assert "Acme" in r.output


def test_no_browser_flag_and_custom_api_url_are_honoured(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from spooling.cli import cli
    cloud, calls = _cli(monkeypatch, tmp_path)
    assert CliRunner().invoke(cli, ["login", "--no-browser", "--api-url", "https://staging.example/"]).exit_code == 0
    assert calls == [("https://staging.example", False)]


def test_cloud_login_with_a_key_still_works_without_the_browser(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from spooling.cli import cli
    cloud, calls = _cli(monkeypatch, tmp_path)
    assert CliRunner().invoke(cli, ["cloud", "login", "--key", "sk_pasted_key"]).exit_code == 0
    assert calls == [] and cloud._load_config()["api_key"] == "sk_pasted_key"


def test_a_failed_login_leaves_the_existing_key_alone(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from spooling.cli import cli
    cloud, _ = _cli(monkeypatch, tmp_path)
    cloud._save_config({"api_key": "sk_old"})
    import spooling.device_login as dl
    monkeypatch.setattr(dl, "run_device_login", lambda *a, **k: (_ for _ in ()).throw(LoginError("denied in the browser")))
    r = CliRunner().invoke(cli, ["login"])
    assert r.exit_code != 0 and "denied" in r.output
    assert cloud._load_config()["api_key"] == "sk_old"
