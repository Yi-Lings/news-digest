import os
from pathlib import Path
from unittest.mock import call, patch

from news_digest.admin_providers import save_profiles, update_profiles
from news_digest.config_io import atomic_write_text

ROOT = Path(__file__).parents[2]
PROVIDER = {
    "name": "default",
    "base_url": "https://api.example.com/v1",
    "api_key": "test-key",
    "model": "test-model",
    "api_type": "openai_chat",
    "stream": True,
    "reasoning_effort": "",
    "enabled": True,
    "is_default": True,
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_bootstrap_shares_only_provider_config_with_worker_group():
    bootstrap = _read("deploy/bootstrap.sh")
    compose = _read("deploy/compose.yaml")

    assert 'chown root:10001 "$CONFIG_DIR"' in bootstrap
    assert 'chmod 750 "$CONFIG_DIR"' in bootstrap
    assert bootstrap.count('chown root:10001 "$PROVIDERS_FILE"') == 2
    assert bootstrap.count('chmod 640 "$PROVIDERS_FILE"') == 2
    assert 'chown root:root "$ENV_FILE"' in bootstrap
    assert 'chmod 600 "$ENV_FILE"' in bootstrap
    assert '"${CONFIG_DIR}/session-secret"' in bootstrap
    assert '"${CONFIG_DIR}/admin-password.initial"' in bootstrap
    assert 'chown root:root "$private_file"' in bootstrap
    assert 'chmod 600 "$private_file"' in bootstrap
    assert 'user: "10001:10001"' in compose
    assert '/srv/news-digest/config:/config:ro' in compose


def test_bootstrap_prepares_shared_data_volume_before_starting_admin():
    bootstrap = _read("deploy/bootstrap.sh")
    compose = _read("deploy/compose.yaml")

    function_start = bootstrap.index("prepare_shared_data_volume()")
    call = bootstrap.index("\nprepare_shared_data_volume\n", function_start)
    admin = bootstrap.index('"${COMPOSE[@]}" up -d web site admin', call)
    function_body = bootstrap[function_start:call]

    assert call < admin
    assert 'DATA_VOLUME="news-digest_news-data"' in function_body
    assert 'docker volume create "$DATA_VOLUME"' in function_body
    assert "--network none --user 0:0" in function_body
    assert 'chgrp -R 10001 /data' in function_body
    assert 'chmod -R g+rwX /data' in function_body
    assert "find /data -type d -exec chmod g+s {} +" in function_body
    assert "news-data:\n    # bootstrap" in compose
    assert "external: true\n    name: news-digest_news-data" in compose


def test_admin_provider_replace_is_group_readable_but_secrets_remain_private(tmp_path):
    provider_path = tmp_path / "providers.json"
    secret_path = tmp_path / "session-secret"

    with patch("news_digest.config_io.os.chmod", wraps=os.chmod) as chmod:
        save_profiles(tmp_path, {"providers": {"default": PROVIDER}}, "providers.json")
        update_profiles(tmp_path, lambda profiles: None, "providers.json")
        atomic_write_text(secret_path, "secret")

    assert chmod.call_args_list.count(call(provider_path, 0o640)) == 2
    chmod.assert_any_call(secret_path, 0o600)
