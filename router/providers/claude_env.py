import re
from typing import Dict


_ENV_TEMPLATE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def configured_claude_env_profiles(config: dict | None) -> Dict[str, dict]:
    raw_profiles = (config or {}).get("claude_env_profiles")
    if not isinstance(raw_profiles, dict):
        return {}
    profiles: Dict[str, dict] = {}
    for raw_name, raw_profile in raw_profiles.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw_profile, dict):
            continue
        normalized: dict[str, str] = {}
        for raw_key, raw_value in raw_profile.items():
            key = str(raw_key or "").strip()
            if not key or raw_value is None:
                continue
            normalized[key] = str(raw_value)
        if normalized:
            profiles[name] = normalized
    return profiles


def expand_env_templates(value: str, env_source: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        env_key = str(match.group(1) or "").strip()
        if not env_key:
            return ""
        resolved = env_source.get(env_key)
        if resolved is None:
            raise RuntimeError(
                f"Missing environment variable '{env_key}' required for Claude environment profile"
            )
        return str(resolved)

    return _ENV_TEMPLATE_PATTERN.sub(repl, str(value))


def resolve_claude_profile_env(
    provider_config: dict | None,
    launch_params: dict,
    base_env: dict[str, str],
) -> dict[str, str]:
    profile_name = str(launch_params.get("claude_env_profile") or "").strip()
    if not profile_name:
        return {}
    profiles = configured_claude_env_profiles(provider_config)
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(profiles.keys()))
        detail = f" Available profiles: {available}." if available else ""
        raise RuntimeError(f"Unknown Claude environment profile '{profile_name}'.{detail}")
    resolved: dict[str, str] = {}
    env_source = dict(base_env)
    for key, value in profile.items():
        expanded = expand_env_templates(value, env_source)
        resolved[str(key)] = expanded
        env_source[str(key)] = expanded
    return resolved


def resolve_claude_env_overrides(launch_params: dict, base_env: dict[str, str]) -> dict[str, str]:
    raw_env = launch_params.get("claude_env")
    if not isinstance(raw_env, dict):
        return {}
    resolved: dict[str, str] = {}
    env_source = dict(base_env)
    for raw_key, raw_value in raw_env.items():
        key = str(raw_key or "").strip()
        if not key or raw_value is None:
            continue
        expanded = expand_env_templates(str(raw_value), env_source)
        resolved[key] = expanded
        env_source[key] = expanded
    return resolved


def resolve_claude_profile_model(provider_config: dict | None, profile_name: str | None) -> str | None:
    text = str(profile_name or "").strip()
    if not text:
        return None
    profile = configured_claude_env_profiles(provider_config).get(text)
    if not isinstance(profile, dict):
        return None
    for key in (
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        value = str(profile.get(key) or "").strip()
        if value:
            return value
    return None
