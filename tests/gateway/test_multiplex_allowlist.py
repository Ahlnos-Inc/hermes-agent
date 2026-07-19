"""BUILD-554: multiplex_profiles list form = allowlisted multiplex."""
import yaml

from gateway.config import GatewayConfig


def _cfg(tmp_path, yaml_text):
    return GatewayConfig.from_dict(yaml.safe_load(yaml_text) or {})


def test_bool_form_unchanged(tmp_path):
    cfg = _cfg(tmp_path, "gateway:\n  multiplex_profiles: true\n")
    assert cfg.multiplex_profiles is True
    assert cfg.multiplex_profile_allowlist is None


def test_list_form_enables_with_allowlist(tmp_path):
    cfg = _cfg(tmp_path, "gateway:\n  multiplex_profiles: [marketing-operator, dross]\n")
    assert cfg.multiplex_profiles is True
    assert cfg.multiplex_profile_allowlist == ("marketing-operator", "dross")


def test_empty_list_means_off(tmp_path):
    cfg = _cfg(tmp_path, "gateway:\n  multiplex_profiles: []\n")
    assert cfg.multiplex_profiles is False


def test_default_off(tmp_path):
    cfg = _cfg(tmp_path, "agent: {}\n")
    assert cfg.multiplex_profiles is False
    assert cfg.multiplex_profile_allowlist is None
