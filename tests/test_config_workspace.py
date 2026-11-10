import json

import pytest

from kaki.config import Config, load_config, save_config
from kaki.types import KakiError
from kaki.workspace import WorkspaceLease


def test_aliases_precedence_blank_env_and_secret(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "openai": {
                        "apiBase": "https://gateway.test/v1",
                        "apiKey": "secret-value",
                        "extraHeaders": None,
                    }
                },
                "agents": {"defaults": {"model": "file"}},
            }
        )
    )
    monkeypatch.setenv("KAKI_AGENTS__DEFAULTS__MODEL", "")
    cfg = load_config(path)
    assert cfg.agents.defaults.model == "file"
    monkeypatch.setenv("KAKI_AGENTS__DEFAULTS__MODEL", "env")
    assert load_config(path).agents.defaults.model == "env"
    assert load_config(path, model="cli").agents.defaults.model == "cli"
    save_config(cfg, path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config(path).providers.openai.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in str(cfg)


def test_invalid_config_not_overwritten(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"providers": {"openai": {"apiKey": "sk-sensitive"}}, "unknown": true}')
    with pytest.raises(KakiError) as exc:
        load_config(p)
    assert "sk-sensitive" not in str(exc.value)
    assert "unknown" in p.read_text()
    p.write_text("{")
    with pytest.raises(KakiError):
        load_config(p)
    assert p.read_text() == "{"


def test_workspace_escape_and_exclusive_lease(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("private")
    (workspace.root / "link").symlink_to(outside)
    for path in ["../outside", "link", ".git/config", ".env", "service.key"]:
        with pytest.raises(KakiError):
            workspace.path(path)
    first, second = WorkspaceLease(workspace.state), WorkspaceLease(workspace.state)
    first.acquire()
    try:
        with pytest.raises(KakiError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_configuration_refuses_other_mcp():
    with pytest.raises(ValueError):
        Config.model_validate({"tools": {"mcpServers": {"other": {}}}})
