import pytest
from pathlib import Path
from config_loader import load_config, ConfigError

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
markets: [tw, jp]
targets:
  titles: ["Backend Engineer"]
  experience_years: "3-5"
  exclude_keywords: []
sources:
  linkedin_gmail: true
  104: true
notification:
  to: test@example.com
  from: test@example.com
""")
    cfg = load_config(cfg_file)
    assert cfg["markets"] == ["tw", "jp"]
    assert cfg["targets"]["titles"] == ["Backend Engineer"]

def test_load_config_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("nonexistent.yaml"))

def test_load_config_missing_required_key(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("markets: [tw]")
    with pytest.raises(ConfigError, match="targets"):
        load_config(cfg_file)
