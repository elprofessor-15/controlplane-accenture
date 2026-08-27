import os
import tempfile
import yaml
from typing import Dict, Any

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
RUNTIME_CONFIG_CACHE: Dict[str, Dict[str, Any]] = {}

def load_all_configs() -> Dict[str, Dict[str, Any]]:
    global RUNTIME_CONFIG_CACHE
    configs = {}
    if os.path.exists(CONFIG_DIR):
        for fname in os.listdir(CONFIG_DIR):
            if fname.endswith(".yaml") or fname.endswith(".yml"):
                fpath = os.path.join(CONFIG_DIR, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    configs[data["use_case_id"]] = data
    RUNTIME_CONFIG_CACHE = configs
    return configs

def get_config(use_case_id: str) -> Dict[str, Any]:
    global RUNTIME_CONFIG_CACHE
    if not RUNTIME_CONFIG_CACHE or use_case_id not in RUNTIME_CONFIG_CACHE:
        load_all_configs()
    return RUNTIME_CONFIG_CACHE.get(use_case_id, {})

def update_runtime_config(use_case_id: str, new_config: Dict[str, Any]) -> None:
    global RUNTIME_CONFIG_CACHE
    RUNTIME_CONFIG_CACHE[use_case_id] = new_config
    fpath = os.path.join(CONFIG_DIR, f"{use_case_id}.yaml")
    fd, temporary_path = tempfile.mkstemp(prefix=f".{use_case_id}.", suffix=".yaml", dir=CONFIG_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(new_config, f, default_flow_style=False, sort_keys=False)
        os.replace(temporary_path, fpath)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise