import yaml
import os

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")

with open(_SETTINGS_PATH) as _f:
    SETTINGS: dict = yaml.safe_load(_f)
