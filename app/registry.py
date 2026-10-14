"""加载 registry/agents.yaml 工具注册表。"""

from functools import lru_cache
from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "registry" / "agents.yaml"


@lru_cache(maxsize=1)
def load_agents() -> list[dict]:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("agents", [])


def get_agent(agent_id: str) -> dict | None:
    for a in load_agents():
        if a["id"] == agent_id:
            return a
    return None
