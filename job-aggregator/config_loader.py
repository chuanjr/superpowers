from pathlib import Path
import yaml


REQUIRED_KEYS = ["markets", "targets", "sources", "notification"]


class ConfigError(Exception):
    pass


def load_config(path: Path | str = "config.yaml") -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for key in REQUIRED_KEYS:
        if key not in cfg:
            raise ConfigError(f"Missing required config key: {key}")
    return cfg
