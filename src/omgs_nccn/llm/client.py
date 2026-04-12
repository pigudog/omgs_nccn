"""Lightweight multi-provider LLM client initialization for omgs_nccn."""

from __future__ import annotations

import os
from typing import Any
from typing import Dict
from typing import Literal
from typing import Optional

from .wrapper import LocalTransformersWrapper
from .wrapper import OpenAIWrapper


def _env_true(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _classify_model_id(model: str) -> str:
    normalized = str(model or "").strip()
    if normalized == "qwen-2.5-3b":
        return "qwen_local"
    if normalized.startswith("gpt-"):
        return "openai_native"
    if normalized in {"qwen", "qwen_compat"} or normalized.startswith("qwen"):
        return "qwen_family"
    if "/" in normalized:
        return "provider_prefixed"
    return "plain_unknown"


def _get_provider_config() -> Dict[str, Dict[str, Any]]:
    return {
        "azure": {
            "api_key_env": "AZURE_OPENAI_API_KEY",
            "endpoint_env": "AZURE_OPENAI_ENDPOINT",
            "deployment_env": "AZURE_OPENAI_GPT5_DEPLOYMENT",
            "default_model": os.getenv("OMGS_NCCN_AZURE_GPT5_MODEL", "gpt-5.1"),
            "base_url": os.getenv(
                "AZURE_OPENAI_BASE_URL",
                "https://api.openai.azure.com/openai/v1/",
            ),
        },
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://api.openai.com/v1",
        },
        "openrouter": {
            "api_key_env": "OPENROUTER_API_KEY",
            "base_url": "https://openrouter.ai/api/v1",
        },
        "qwen_compat": {
            "api_key_env": "QWEN_COMPAT_API_KEY",
            "base_url_env": "QWEN_COMPAT_BASE_URL",
            "default_model_env": "QWEN_COMPAT_MODEL",
            "default_model": "qwen3-max",
        },
        "qwen-2.5-3b": {
            "model_path_env": "LOCAL_QWEN25_3B_MODEL_PATH",
            "default_model": "qwen-2.5-3b",
        },
    }


def _default_db_path() -> str:
    return os.getenv("OMGS_NCCN_API_TRACE_DB", "omgs_nccn_api_trace.db")


def init_client(
    db_path: Optional[str] = None,
    provider: Literal["azure", "openai", "openrouter", "qwen_compat", "qwen", "qwen-2.5-3b"] = "azure",
) -> OpenAIWrapper | LocalTransformersWrapper:
    if db_path is None:
        db_path = _default_db_path()

    provider_aliases = {
        "qwen": "qwen_compat",
    }
    provider = provider_aliases.get(provider, provider)

    provider_configs = _get_provider_config()
    if provider not in provider_configs:
        raise ValueError(
            f"Unknown provider: {provider}. Supported: {list(provider_configs.keys())}"
        )

    config = provider_configs[provider]

    if provider == "azure":
        endpoint = os.getenv(config.get("endpoint_env", "AZURE_OPENAI_ENDPOINT"))
        api_key = os.getenv(config.get("api_key_env", "AZURE_OPENAI_API_KEY"))
        endpoint = (endpoint or "").rstrip("/")
        if endpoint and "/openai/" not in endpoint:
            endpoint = f"{endpoint}/openai/v1/"

        if not endpoint or not api_key:
            raise RuntimeError(
                f"Missing {config.get('endpoint_env')} or {config.get('api_key_env')} "
                "environment variables for Azure OpenAI."
            )

        return OpenAIWrapper(
            api_key=api_key,
            base_url=endpoint,
            db_path=db_path,
            provider="azure",
        )

    if provider == "openai":
        api_key = os.getenv(config.get("api_key_env", "OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError(
                f"Missing {config.get('api_key_env')} environment variable for OpenAI."
            )
        return OpenAIWrapper(
            api_key=api_key,
            base_url=config.get("base_url"),
            db_path=db_path,
            provider="openai",
        )

    if provider == "openrouter":
        api_key = os.getenv(config.get("api_key_env", "OPENROUTER_API_KEY"))
        if not api_key:
            raise RuntimeError(
                f"Missing {config.get('api_key_env')} environment variable for OpenRouter."
            )
        return OpenAIWrapper(
            api_key=api_key,
            base_url=config.get("base_url"),
            db_path=db_path,
            provider="openrouter",
        )

    if provider == "qwen_compat":
        api_key = os.getenv(config.get("api_key_env", "QWEN_COMPAT_API_KEY"))
        base_url = os.getenv(config.get("base_url_env", "QWEN_COMPAT_BASE_URL"), "").strip()
        if not api_key or not base_url:
            raise RuntimeError(
                "Missing qwen compatible API settings. "
                "Set QWEN_COMPAT_API_KEY and QWEN_COMPAT_BASE_URL."
            )
        return OpenAIWrapper(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            db_path=db_path,
            provider="qwen_compat",
        )

    if provider == "qwen-2.5-3b":
        model_path_env = config.get("model_path_env", "LOCAL_QWEN25_3B_MODEL_PATH")
        model_path = os.getenv(model_path_env, "").strip()
        if not model_path:
            raise RuntimeError(
                f"Missing local qwen model path for qwen-2.5-3b. Set {model_path_env}."
            )
        return LocalTransformersWrapper(
            model_path=model_path,
            db_path=db_path,
            provider="qwen-2.5-3b",
        )

    raise ValueError(f"Unsupported provider: {provider}")


def init_client_from_config(
    model: str,
    db_path: Optional[str] = None,
) -> OpenAIWrapper | LocalTransformersWrapper:
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_key = os.getenv("AZURE_OPENAI_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    qwen_key = os.getenv("QWEN_COMPAT_API_KEY")
    qwen_base_url = os.getenv("QWEN_COMPAT_BASE_URL")

    azure_preferred = _env_true(os.getenv("OMGS_NCCN_FORCE_AZURE", "1"))
    model_kind = _classify_model_id(model)

    if model_kind == "qwen_local":
        return init_client(db_path=db_path, provider="qwen-2.5-3b")

    if model_kind == "qwen_family":
        if qwen_key and qwen_base_url:
            return init_client(db_path=db_path, provider="qwen_compat")
        if openrouter_key:
            return init_client(db_path=db_path, provider="openrouter")
        return init_client(db_path=db_path, provider="qwen_compat")

    if model_kind == "provider_prefixed":
        if openrouter_key:
            return init_client(db_path=db_path, provider="openrouter")
        if azure_preferred and azure_endpoint and azure_key:
            return init_client(db_path=db_path, provider="azure")
        return init_client(db_path=db_path, provider="openai")

    if model_kind == "openai_native" or azure_preferred:
        if azure_endpoint and azure_key:
            return init_client(db_path=db_path, provider="azure")
        if openai_key:
            return init_client(db_path=db_path, provider="openai")
        if openrouter_key:
            return init_client(db_path=db_path, provider="openrouter")

    if azure_endpoint and azure_key:
        return init_client(db_path=db_path, provider="azure")
    if openai_key:
        return init_client(db_path=db_path, provider="openai")
    if openrouter_key:
        return init_client(db_path=db_path, provider="openrouter")

    return init_client(db_path=db_path, provider="azure")
