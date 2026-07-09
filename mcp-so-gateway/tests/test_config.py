from so_gateway.config import load_config


def test_load_config_reads_env(monkeypatch):
    monkeypatch.setenv("SO_URL", "https://so.test")
    monkeypatch.setenv("SO_EMAIL", "soc-agent@securityonion.local")
    monkeypatch.setenv("SO_PASSWORD", "pw")
    cfg = load_config()
    assert cfg.url == "https://so.test"
    assert cfg.email == "soc-agent@securityonion.local"
    assert cfg.password == "pw"
    assert cfg.ssl_skip_verify is False  # default when env unset


def test_ssl_skip_verify_true(monkeypatch):
    monkeypatch.setenv("SO_URL", "https://so.test")
    monkeypatch.setenv("SO_EMAIL", "soc-agent@securityonion.local")
    monkeypatch.setenv("SO_PASSWORD", "pw")
    monkeypatch.setenv("SO_SSL_SKIP_VERIFY", "true")
    cfg = load_config()
    assert cfg.ssl_skip_verify is True
