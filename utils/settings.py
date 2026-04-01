import logging
import os

import yaml


def load_config(config_file="config.yaml"):
    if not os.path.exists(config_file):
        return {}
    try:
        with open(config_file, "r", encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except Exception as exc:
        logging.error(f"Failed to load config file {config_file}: {exc}")

    return {}


def resolve_github_token(cli_token=None, config=None):
    if cli_token:
        return cli_token
    if config:
        return config.get("GITHUB_TOKEN")
    return None


def feature_enabled(config, key):
    if not config:
        return False
    return bool(config.get("fetch_all", False) or config.get(key, False))


def get_path_config(config, key, default=None):
    if not config:
        return default
    value = config.get("paths", {}).get(key) or default
    if value is None:
        return None
    if os.path.isabs(value):
        return value

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.path.join(project_root, value))
