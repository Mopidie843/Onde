"""Accès à distance (Tailscale) : lecture de l'état. Les sorties JSON ci-dessous sont celles de `tailscale`."""
from __future__ import annotations

from onde import config, remote

STATUS = {"BackendState": "Running",
          "Self": {"DNSName": "onde.tail1234.ts.net.", "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"]}}


def serve(funnel: bool, port: int = 8000):
    d = {"TCP": {"443": {"HTTPS": True}},
         "Web": {"onde.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}
    if funnel:
        d["AllowFunnel"] = {"onde.tail1234.ts.net:443": True}
    return d


def test_private():
    st = remote.parse(STATUS, serve(False))
    assert st["mode"] == "private" and st["url"] == "https://onde.tail1234.ts.net" and st["ip"] == "100.101.102.103"


def test_public():
    st = remote.parse(STATUS, serve(True))
    assert st["mode"] == "public" and st["url"] == "https://onde.tail1234.ts.net"


def test_connected_but_not_shared_falls_back_to_tailnet_address():
    st = remote.parse(STATUS, {})
    assert st["mode"] == "private" and st["url"] == f"http://100.101.102.103:{config.PORT}"


def test_other_service_on_tailscale_is_not_ours():
    st = remote.parse(STATUS, serve(True, port=3000))
    assert st["mode"] != "public" and not (st["url"] or "").startswith("https://")


def test_not_connected_or_missing():
    assert remote.parse({"BackendState": "NeedsLogin"}, None)["running"] is False
    assert remote.parse(None, None)["mode"] == "off"


def test_warning_when_public_without_password(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda n: "/usr/bin/tailscale")
    monkeypatch.setattr(remote, "_json", lambda a: STATUS if a[0] == "status" else serve(True))
    monkeypatch.setattr(remote, "_protected", lambda: False)
    assert "sans mot de passe" in remote.status(force=True)["warning"]
    monkeypatch.setattr(remote, "_protected", lambda: True)
    assert remote.status(force=True)["warning"] is None


def test_not_installed(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda n: None)
    st = remote.status(force=True)
    assert st["installed"] is False and "./start.sh remote" in remote.describe(st)[0]


def test_tunnel_without_tailscale(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(remote.shutil, "which", lambda n: None)  # ni tailscale ni pgrep : on simule le tunnel plus bas
    (tmp_path / "tunnel.log").write_text("INF +----+\nINF https://vieux-nom.trycloudflare.com\nINF https://beau-nom-1.trycloudflare.com\n")
    monkeypatch.setattr(remote, "_tunnel_running", lambda: True)
    monkeypatch.setattr(remote, "_protected", lambda: True)
    st = remote.status(force=True)
    assert st["mode"] == "public" and st["url"] == "https://beau-nom-1.trycloudflare.com" and st["provider"] == "cloudflare"
    assert st["warning"] is None and "tunnel Cloudflare" in remote.describe(st)[0]


def test_tunnel_log_ignored_when_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "tunnel.log").write_text("https://ancien.trycloudflare.com\n")
    monkeypatch.setattr(remote, "_tunnel_running", lambda: False)
    assert remote.tunnel_url() is None


def test_tunnel_warns_without_password(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(remote.shutil, "which", lambda n: None)
    (tmp_path / "tunnel.log").write_text("https://x-y.trycloudflare.com\n")
    monkeypatch.setattr(remote, "_tunnel_running", lambda: True)
    monkeypatch.setattr(remote, "_protected", lambda: False)
    assert "sans mot de passe" in remote.status(force=True)["warning"]
