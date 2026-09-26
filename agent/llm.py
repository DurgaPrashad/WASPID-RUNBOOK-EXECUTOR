"""LLM provider for the WASPID agent: OpenAI, or any model behind the TrueFoundry AI Gateway.

The TrueFoundry AI Gateway speaks the OpenAI API, so both use the same OpenAI SDK
client; only the base URL, key and model id differ. Keys come from the
environment and are never logged or shown in the console.

  OpenAI       OPENAI_API_KEY  [OPENAI_BASE_URL]  [WASPID_MODEL, default gpt-5-mini]
  TrueFoundry  TFY_API_KEY + TFY_GATEWAY_BASE_URL + WASPID_MODEL (a gateway model id,
               e.g. openai-main/gpt-5-mini)
  Force one    WASPID_LLM_PROVIDER=openai | truefoundry   (default: TrueFoundry if configured)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
PROVIDER_NAMES = {"openai": "OpenAI", "truefoundry": "TrueFoundry AI Gateway"}
NOT_CONFIGURED = ("No LLM configured. Set OPENAI_API_KEY for OpenAI, or TFY_API_KEY + "
                  "TFY_GATEWAY_BASE_URL + WASPID_MODEL for the TrueFoundry AI Gateway.")


class LLMConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMConfig:
    provider: Optional[str]          # "openai" | "truefoundry" | None
    model: Optional[str] = None
    base_url: Optional[str] = None
    error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return self.provider is not None

    def describe(self) -> str:
        if not self.configured:
            return self.error or "not configured"
        return f"{PROVIDER_NAMES[self.provider]} · {self.model}"


def detect(env: Mapping[str, str] = os.environ) -> LLMConfig:
    forced = env.get("WASPID_LLM_PROVIDER", "").strip().lower()
    has_tfy = bool(env.get("TFY_API_KEY") and env.get("TFY_GATEWAY_BASE_URL"))
    has_openai = bool(env.get("OPENAI_API_KEY"))
    if forced not in ("", "openai", "truefoundry"):
        return LLMConfig(None, error=f"unknown WASPID_LLM_PROVIDER={forced!r} (use openai or truefoundry)")
    if forced == "truefoundry" or (not forced and has_tfy):
        if not has_tfy:
            return LLMConfig(None, error="TrueFoundry needs TFY_API_KEY and TFY_GATEWAY_BASE_URL")
        if not env.get("WASPID_MODEL"):
            return LLMConfig(None, error="set WASPID_MODEL to a TrueFoundry AI Gateway model id")
        return LLMConfig("truefoundry", env["WASPID_MODEL"], env["TFY_GATEWAY_BASE_URL"])
    if forced == "openai" or has_openai:
        if not has_openai:
            return LLMConfig(None, error="OpenAI needs OPENAI_API_KEY")
        return LLMConfig("openai", env.get("WASPID_MODEL") or DEFAULT_OPENAI_MODEL, env.get("OPENAI_BASE_URL"))
    return LLMConfig(None)


def make_client(cfg: LLMConfig, env: Mapping[str, str] = os.environ) -> Any:
    if not cfg.configured:
        raise LLMConfigError(cfg.error or NOT_CONFIGURED)
    from openai import OpenAI  # runtime dependency, only for the agent

    key = env["TFY_API_KEY"] if cfg.provider == "truefoundry" else env["OPENAI_API_KEY"]
    return OpenAI(api_key=key, base_url=cfg.base_url)
