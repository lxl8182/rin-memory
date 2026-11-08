"""One explicit TOML configuration; paths are relative to its own directory."""
from __future__ import annotations

import os
import math
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapters.model_http import ModelSettings
from .service.memory_service import ServiceSettings
from .service.memory_service import _with_console_model_config


class StackConfigError(ValueError):
    pass


#: 上下文分段(RO02-CTX)的默认参数。
#:
#: 与 DSH 记忆插件的 `segment` 配置一一对应。两处都写默认值是有意的:插件是
#: 唯一真正按这些数字判断「该不该换段」的地方,而服务端这份用于管理台展示和
#: 配置校验 —— 任何一侧缺失都不该让聊天停下来。
SEGMENT_DEFAULTS = {
    "budget_tokens": 128000,
    "hard_tokens": 160000,
    "carry_turns": 30,
    "carry_tokens": 8000,
    "idle_hours": 24,
    "min_turns": 6,
    "max_age_days": 14,
}


def _segment_table(raw: dict, path: Path) -> dict[str, int]:
    """读取并校验 `[segment]` 表。

    这些数字决定「一次请求能塞多少」。写错一个不会让服务起不来,但会让上限失效,
    所以宁可在这里明确报错,也不静默回落到默认值 —— 用户改了配置却没生效,
    比配置被拒绝更难查。
    """
    table = _table(raw, "segment")
    known = set(SEGMENT_DEFAULTS)
    unknown = sorted(set(table) - known)
    if unknown:
        raise StackConfigError(
            f"segment: unknown key(s) {', '.join(unknown)}; "
            f"expected {', '.join(sorted(known))}",
        )
    values = dict(SEGMENT_DEFAULTS)
    for key, default in SEGMENT_DEFAULTS.items():
        if key not in table:
            continue
        value = table[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StackConfigError(f"segment.{key}: expected a non-negative integer")
        if key in ("budget_tokens", "hard_tokens", "carry_tokens") and value < 1:
            raise StackConfigError(f"segment.{key}: must be positive")
        if key == "idle_hours" and value < 1:
            raise StackConfigError("segment.idle_hours: must be at least 1")
        if key == "max_age_days" and value < 1:
            raise StackConfigError("segment.max_age_days: must be at least 1")
        values[key] = value
    # 两条跨字段约束在语义上是错的,不是「值不好看」:硬上限不高于软上限时软上限
    # 永远先到,硬上限形同虚设;带入上限不小于软上限时新段首次请求自带超限。
    if values["hard_tokens"] <= values["budget_tokens"]:
        raise StackConfigError("segment.hard_tokens: must be greater than segment.budget_tokens")
    if values["carry_tokens"] >= values["budget_tokens"]:
        raise StackConfigError("segment.carry_tokens: must be less than segment.budget_tokens")
    return values


@dataclass(frozen=True)
class StackConfig:
    path: Path
    roots: dict[str, Path]
    service: ServiceSettings
    python: Path
    display_name: str
    subject: str
    request_capacity: int
    startup_timeout_s: float
    host: dict[str, Any]
    segment: dict[str, int]

    @property
    def url(self) -> str:
        return f"http://{self.service.host}:{self.service.port}"

    def missing(self) -> list[str]:
        model = _with_console_model_config(self.service).model
        fields = []
        for tier in ("summary", "correction"):
            if not model.models.get(tier):
                fields.append(f"models.service.tiers.{tier}")
        if not model.base_url:
            fields.append("models.service.base_url")
        if not model.api_key_stored and (not model.api_key_env or not os.environ.get(model.api_key_env)):
            fields.append("models.service.api_key_env (environment variable not set)")
        return fields


def _is_source_tree(python: Path) -> bool:
    """Whether this interpreter resolves `rin_memory` from a checkout.

    Used only to relax the version pin: a checkout's version changes with every
    edit, and the interpreter is running exactly that code. An installed wheel
    keeps the strict check, because there the pin is what guarantees the launcher
    and the package agree.
    """
    try:
        import subprocess

        completed = subprocess.run(
            [str(python), "-c",
             "import rin_memory, pathlib;"
             "print(pathlib.Path(rin_memory.__file__).resolve())"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    module_path = completed.stdout.strip()
    if not module_path:
        return False
    # `<repo>/src/rin_memory/__init__.py` lives beside a `pyproject.toml`
    # in `<repo>`; a wheel install does not.
    package_dir = Path(module_path).parent
    source_root = package_dir.parent
    return (source_root / "pyproject.toml").is_file() and (source_root / "src").is_dir()


def _path(base: Path, value: Any, field: str, *, resolve_symlinks: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise StackConfigError(f"{field}: an explicit path is required")
    path = Path(value).expanduser()
    absolute = path if path.is_absolute() else base / path
    # A POSIX venv selects its environment through the invoked symlink path.
    return absolute.resolve() if resolve_symlinks else absolute.absolute()


def _number(table: dict, key: str, default: float, *, integer=False):
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise StackConfigError(f"service.{key}: must be positive")
    if integer and not isinstance(value, int):
        raise StackConfigError(f"service.{key}: must be an integer")
    return value


def _nonnegative_number(table: dict, key: str, default: float):
    """Like `_number` but 0 is meaningful (RO02 batch idle window).

    For `batch_idle_seconds` the value is a policy selector, not a duration
    that must be positive: 0 means "extract on the next maintenance round".
    """
    value = table.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise StackConfigError(f"service.{key}: must be zero or positive")
    return value


def _table(raw: dict, key: str, prefix: str = "") -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise StackConfigError(f"{prefix}{key}: expected a TOML table")
    return value


def load_config(path: Path | str) -> StackConfig:
    path = Path(path).expanduser().resolve()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StackConfigError(f"{path}: {exc}") from exc
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise StackConfigError("schema_version: expected 1")
    paths = _table(raw, "paths")
    roots = {key: _path(path.parent, paths.get(key), f"paths.{key}")
             for key in ("memory", "host", "logs", "state")}
    for name, root in roots.items():
        if root == Path(root.anchor) or root == path or path.is_relative_to(root):
            raise StackConfigError(f"paths.{name}: must be a dedicated runtime directory")
        for other, other_root in roots.items():
            if name != other and root.is_relative_to(other_root):
                raise StackConfigError(f"paths.{name}: overlaps paths.{other}")
        for parent in (root, *root.parents):
            if (parent / ".git").exists():
                raise StackConfigError(f"paths.{name}: runtime must be outside a source repository")
    character = _table(raw, "character")
    character_id = character.get("character_id", "")
    if not isinstance(character_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", character_id):
        raise StackConfigError("character.character_id: use 1 to 64 letters, digits, _ or -")
    subject = character.get("subject", "private")
    display = character.get("display_name", character_id)
    if not isinstance(subject, str) or not subject.strip():
        raise StackConfigError("character.subject: a nonempty identity is required")
    if not isinstance(display, str) or not display.strip() or len(display) > 80:
        raise StackConfigError("character.display_name: use 1 to 80 characters")
    runtime = _table(raw, "runtime")
    python = _path(path.parent, runtime.get("python"), "runtime.python", resolve_symlinks=False)
    if not python.is_file():
        raise StackConfigError(f"runtime.python: interpreter does not exist: {python}")
    # A pinned build and the installed one must agree; otherwise the launcher is
    # running code the config was not written for. A source checkout is the
    # exception: its version moves on every edit, and the code being run *is*
    # the configured tree, so a mismatch there is expected rather than an error.
    configured_version = runtime.get("package_version")
    installed_version = version("rin-memory")
    if configured_version != installed_version and not _is_source_tree(python):
        raise StackConfigError(
            "runtime.package_version: install the configured build with "
            f"runtime.python (configured {configured_version!r}, "
            f"installed {installed_version!r})",
        )
    cfg = _table(raw, "service")
    host = cfg.get("host", "127.0.0.1")
    if host != "127.0.0.1":
        raise StackConfigError("service.host: this release listens on 127.0.0.1")
    port = _number(cfg, "port", 48931, integer=True)
    if port > 65535:
        raise StackConfigError("service.port: must be at most 65535")
    model = _table(_table(raw, "models"), "service", "models.")
    if "api_key" in model:
        raise StackConfigError("models.service.api_key_env: store an environment variable name, not api_key")
    tiers = model.get("tiers", {})
    if not isinstance(tiers, dict) or any(not isinstance(v, str) for v in tiers.values()):
        raise StackConfigError("models.service.tiers: model names must be strings")
    tiers = {key: value.strip() for key, value in tiers.items() if value.strip()}
    base_url = model.get("base_url", "")
    key_env = model.get("api_key_env", "")
    if not isinstance(base_url, str) or (base_url and urlsplit(base_url).scheme not in ("http", "https")):
        raise StackConfigError("models.service.base_url: expected an HTTP(S) URL")
    if not isinstance(key_env, str) or (key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env)):
        raise StackConfigError("models.service.api_key_env: expected an environment variable name")
    clients = _table(raw, "clients")
    for client, spec in clients.items():
        if client == "console" or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", client) or not isinstance(spec, dict):
            raise StackConfigError("clients: use named client tables; console is reserved")
        if not isinstance(spec.get("token_env"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", spec["token_env"]):
            raise StackConfigError(f"clients.{client}.token_env: expected an environment variable name")
        subjects = spec.get("subjects", [subject])
        if not isinstance(subjects, list) or not subjects or any(not isinstance(item, str) or not item.strip() for item in subjects):
            raise StackConfigError(f"clients.{client}.subjects: expected a nonempty list of identities")
        if not isinstance(spec.get("plugin_id", ""), str):
            raise StackConfigError(f"clients.{client}.plugin_id: expected a string")
        if not isinstance(spec.get("host_persona", False), bool):
            raise StackConfigError(f"clients.{client}.host_persona: expected a boolean")
        if not isinstance(spec.get("memory_tools", False), bool):
            raise StackConfigError(f"clients.{client}.memory_tools: expected a boolean")
        if not isinstance(spec.get("persona_teach", False), bool):
            raise StackConfigError(f"clients.{client}.persona_teach: expected a boolean")
        if not isinstance(spec.get("memory_outputs", False), bool):
            raise StackConfigError(f"clients.{client}.memory_outputs: expected a boolean")
        # M6-A: the plugin ids this client may cite as a saved-message source when
        # it submits an explicit teaching. Empty (the default) trusts no plugin,
        # so an upgrade alone never widens the QQ path.
        trusted = spec.get("trusted_plugins", [])
        if not isinstance(trusted, list) or any(
            not isinstance(item, str) or not item.strip() for item in trusted
        ):
            raise StackConfigError(
                f"clients.{client}.trusted_plugins: expected a list of plugin ids",
            )
    if not isinstance(cfg.get("memory_enabled", True), bool):
        raise StackConfigError("service.memory_enabled: expected a boolean")
    settings = ServiceSettings(
        memory_root=roots["memory"], local_state_root=roots["state"] / "memory-service",
        character=character_id, host=host, port=port,
        maintenance_interval_s=_number(cfg, "maintenance_interval_s", 60),
        chat_timeout_s=_number(cfg, "chat_timeout_s", 60),
        context_budget_ms=_number(cfg, "context_budget_ms", 9000, integer=True),
        max_body_bytes=_number(cfg, "max_body_bytes", 2 * 1024 * 1024, integer=True),
        max_in_flight_chat=_number(cfg, "max_in_flight_chat", 4, integer=True),
        shutdown_timeout_s=_number(cfg, "shutdown_timeout_s", 30),
        require_published_persona=True,
        clients=clients,
        max_pending_events=_number(cfg, "max_pending_events", 1024, integer=True),
        # RO02 cost switch, not an optional nicety: without it production pays
        # one extraction call per turn. Read from [service] so an existing
        # stack.toml keeps working; the shipped template sets 120 explicitly.
        batch_idle_seconds=_nonnegative_number(cfg, "batch_idle_seconds", 120),
        batch_max_turns=_number(cfg, "batch_max_turns", 5, integer=True),
        batch_max_messages=_number(cfg, "batch_max_messages", 16, integer=True),
        memory_enabled=cfg.get("memory_enabled", True),
        model=ModelSettings(
            base_url=base_url, api_key_env=key_env, models=tiers,
            timeout_s=_number(cfg, "model_timeout_s", 60),
            max_concurrency=_number(cfg, "model_concurrency", 4, integer=True),
            proxy=model.get("proxy", ""),
        ),
    )
    host_config = _table(raw, "host")
    host_config["model_settings"] = _table(_table(raw, "models"), "host", "models.")
    presentation = _table(raw, "presentation")
    host_config["live2d_model"] = presentation.get("live2d_model", "")
    host_config["voice_id"] = presentation.get("voice_id", "")
    if not isinstance(host_config.get("enabled", False), bool):
        raise StackConfigError("host.enabled: expected a boolean")
    for key in ("source", "python", "shell", "plugin_source"):
        if host_config.get(key):
            resource = _path(path.parent, host_config[key], f"host.{key}", resolve_symlinks=key != "python")
            if not resource.exists():
                raise StackConfigError(f"host.{key}: path does not exist: {resource}")
            host_config[key] = str(resource)
    if _table(raw, "evolution").get("initial_mode", "off") != "off":
        raise StackConfigError("evolution.initial_mode: initialize as off; select propose/auto in the console")
    return StackConfig(
        path, roots, settings, python, display.strip(), subject.strip(),
        _number(cfg, "request_capacity", 16, integer=True),
        _number(cfg, "startup_timeout_s", 20), host_config,
        _segment_table(raw, path),
    )


def initialize(path: Path | str) -> StackConfig:
    path = Path(path).expanduser().resolve()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # TOML literal strings preserve Windows backslashes without interpolation.
        template = f'''schema_version = 1

[runtime]
python = '{sys.executable}'
package_version = '{version("rin-memory")}'

[paths]
memory = '../memory'
host = '../host'
logs = '../logs'
state = '../state'

[character]
character_id = 'companion'
display_name = '新伙伴'
subject = 'private'

[service]
host = '127.0.0.1'
port = 48931
context_budget_ms = 9000
chat_timeout_s = 60
model_timeout_s = 60
model_concurrency = 4
request_capacity = 16
max_in_flight_chat = 4
max_pending_events = 1024
max_body_bytes = 2097152
startup_timeout_s = 20
shutdown_timeout_s = 30
maintenance_interval_s = 60
# 暂停长期记忆提取、召回和后台加工；人格与普通聊天继续工作，已有数据保留。
memory_enabled = true

# RO02 成本优化开关（不是可选项）：普通提取合批。同一作用域积累 5 轮，或
# 最后一次用户发言后空闲 batch_idle_seconds 秒，满足其一即处理；0 表示每个
# 维护轮立刻处理（旧行为，调用次数多）。原始轮次先落盘再入队，任何取值都不会
# 丢事实，只会影响"多久变成长期记忆"。
batch_idle_seconds = 120
batch_max_turns = 5
batch_max_messages = 16

[models.service]
base_url = ''
api_key_env = 'COMPANION_MODEL_API_KEY'
proxy = ''

[models.service.tiers]
summary = ''
correction = ''
conversation = ''

[host]
# Install the companion_host.py adapter, then set source/python and enable.
enabled = false
client_id = 'neko-desktop'
source = ''
python = ''
shell = ''
plugin_source = ''
port = 48911
memory_port = 48912
tool_port = 48915
plugin_port = 48916
startup_timeout_s = 120

[clients.neko-desktop]
token_env = 'COMPANION_NEKO_TOKEN'
subjects = ['private']
# The desktop host may read, revise and publish the semantic persona card.
# Off by default; enabling it is an explicit grant of that capability only.
host_persona = false
# A chat client may let the user correct or forget their own facts from inside
# the conversation. Independent of host_persona and limited to the subjects
# above; off by default.
memory_tools = false
# A chat client may submit an explicit teaching ("以后别每次都总结") that points
# at one of its own saved messages, and take one back. It cannot post rule
# text or edit any other section. Independent of the two grants above; off by
# default.
persona_teach = false
# A chat client may record assistant output (a proactive message or a task
# result) and report which parts the platform actually delivered. Only the
# delivered parts become shared conversation, so this grant cannot fabricate an
# experience; it can only state what really arrived. Off by default.
memory_outputs = false
# Plugin ids whose saved messages this client may cite when it submits an
# explicit teaching. Only consulted together with persona_teach: a plugin id
# never grants the capability, and it never widens the subject or character
# scope. Empty means "trust no plugin", which keeps the previous behaviour
# (plugin sources refused) until a specific plugin is granted on purpose.
trusted_plugins = []

[models.host]
base_url = ''
api_key_env = 'COMPANION_HOST_API_KEY'
conversation = ''
realtime = ''

[presentation]
live2d_model = ''
voice_provider = ''
voice_id = ''
enabled_plugins = []

[evolution]
# The service persists off/propose/auto; change it in the management console.
initial_mode = 'off'

# 上下文分段(RO02-CTX)。长聊到预算就把旧会话归档、开一段新会话，模型每次
# 只读当前段 + 本轮需要的旧记忆；原始聊天始终完整留在旧会话里，不做摘要改写。
[segment]
# 软上限：本段累计输入达到它，就在本轮结束、气泡全部投递完成后换段。
budget_tokens = 128000
# 硬上限：防一条超长文本或大图冲破预算；触发时不看最少轮数。
hard_tokens = 160000
# 新段带入的最近完整对话轮数，与 carry_tokens 取先到；永不拆半轮。
carry_turns = 30
# 带入正文的 token 上限。
carry_tokens = 8000
# 没有真实用户互动满这么多小时就换段；主动消息与后台任务不重置计时。
idle_hours = 24
# 少于此轮数不换段，防止刚开段就被换掉。
min_turns = 6
# 段龄上限（天），仅在空闲时生效，不会打断进行中的对话。
max_age_days = 14
'''
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(template)
        except FileExistsError:
            pass
    config = load_config(path)
    for root in config.roots.values():
        root.mkdir(parents=True, exist_ok=True)
    return config
