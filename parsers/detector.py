"""
Detector — picks the right SmartParser config for a given file.
No more individual parser files. Everything lives in base.BANK_CONFIGS.
"""

import os
import yaml
from parsers.base import SmartParser, BANK_CONFIGS

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


def load_config():
    with open(os.path.join(CONFIG_DIR, "accounts.yaml")) as f:
        accounts = yaml.safe_load(f)
    with open(os.path.join(CONFIG_DIR, "passwords.yaml")) as f:
        passwords = yaml.safe_load(f)
    return accounts, passwords


def _get_source(accounts_cfg: dict, account_id: str) -> tuple[str, str]:
    all_sources = accounts_cfg.get("accounts", []) + accounts_cfg.get("credit_cards", [])
    for acc in all_sources:
        if acc["id"] == account_id:
            return acc["id"], acc["label"]
    return account_id, account_id


def get_parser_and_password(file_path: str) -> tuple[SmartParser, str]:
    """
    Given a file path, return (SmartParser instance, password).
    Tries each bank config in order — credit cards first (more specific).
    """
    accounts_cfg, passwords_cfg = load_config()
    pwd_map = passwords_cfg.get("pdf_passwords", {})

    # Credit cards before banks; tataneu_cc before hdfc_cc (both match "hdfc" filenames)
    ordered_keys = ["tataneu_cc", "hdfc_cc", "icici_cc", "axis_cc", "sbi", "hdfc_bank", "bob", "canara"]

    for key in ordered_keys:
        cfg = BANK_CONFIGS[key]
        account_id = cfg["account_id"]
        source_id, source_label = _get_source(accounts_cfg, account_id)
        parser = SmartParser(key, source_id, source_label, cfg)
        if parser.can_parse(file_path):
            password = pwd_map.get(cfg["password_key"], "")
            return parser, password

    raise ValueError(
        f"Could not detect parser for: {os.path.basename(file_path)}\n"
        "Make sure the bank name is in the filename, e.g.:\n"
        "  sbi_jan2025.pdf / hdfc_cc_jan2025.pdf / canara_jan2025.xlsx"
    )