"""Custom models: point Naga at a model server you run yourself, by port or by URL.

Targets: ``8000`` (this machine), ``host:8000``, ``https://server.example.com``, a proxied prefix such as
``https://server/mymodel``, or a full endpoint URL like ``https://server/v1/chat/completions`` or
``http://server:8000/generate``. API keys are read from an environment variable (``--api-key-env``) and are never
written to the config file.

``detect`` works out what API the server speaks:

1. OpenAI-compatible: ``GET /v1/models`` answers (vLLM, llama.cpp, LM Studio, most FastAPI wrappers), or the
   server's ``/openapi.json`` lists ``/v1/chat/completions``.
2. Ollama-style: ``GET /api/tags`` answers.
3. A plain endpoint (``simple_http``): the path you give, or a POST route found in ``/openapi.json`` whose name
   looks like generation (``/generate``, ``/chat``, ``/predict``, ...).

The result is saved in the user config as provider ``custom-<name>`` plus a model profile ``<name>``, so it can
be selected later with ``/model``, ``/custom-model use <name>`` or ``naga --profile <name>``.
"""

from __future__ import annotations

import re
import tomllib
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any

import httpx

from .providers.simple_http import extract_text
from .config import Config, set_user_value, unset_user_value, user_config_path

BUILTIN_PROFILES = {"coding", "fast", "reasoning", "vision", "subagent"}
GEN_PATH = re.compile(r"generat|chat|complet|predict|infer|ask|query|respond|answer|invoke|run", re.I)
INPUT_FIELDS = ("messages", "prompt", "message", "text", "input", "query", "question", "inputs")


@dataclass
class Detected:
    kind: str                       # openai_compatible | ollama | simple_http
    base_url: str
    models: list[str] = field(default_factory=list)
    context_length: int | None = None
    path: str | None = None         # simple_http only
    input_field: str = "prompt"     # simple_http only
    native_tools: bool = False
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == "simple_http":
            return f"plain endpoint POST {self.base_url}{self.path} (field '{self.input_field}')"
        api = {"openai_compatible": "OpenAI-compatible API", "ollama": "Ollama API"}[self.kind]
        return f"{api} at {self.base_url}" + (f" serving {', '.join(self.models[:5])}" if self.models else "")


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# URL endings that already say which API it is; whatever comes before them is the base URL.
KNOWN_SUFFIXES = (("/v1/chat/completions", "openai"), ("/v1/completions", "openai"), ("/v1/models", "openai"),
                  ("/chat/completions", "openai"), ("/v1", "openai"),
                  ("/api/generate", "ollama"), ("/api/chat", "ollama"), ("/api/tags", "ollama"))


@dataclass
class Target:
    base: str                 # scheme://host[:port][/prefix]
    path: str | None = None   # extra URL path that may be an endpoint or a prefix
    hint: str | None = None   # "openai" | "ollama" when the URL itself says so
    remote: bool = False      # not this machine: prompts (and code) leave it

    @property
    def url(self) -> str:
        return self.base + (self.path or "")


def parse_target(target: str) -> Target:
    """'8000' -> local port; 'host:8000', 'https://server/x/v1/chat/completions' etc. are URLs."""
    t = target.strip().rstrip("/")
    if t.isdigit():
        port = int(t)
        if not 1 <= port <= 65535:
            raise ValueError(f"invalid port {t}")
        return Target(f"http://127.0.0.1:{port}")
    if "://" not in t:
        t = "http://" + t
    u = urlsplit(t)
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise ValueError(f"not a valid http(s) URL or port: {target}")
    origin = f"{u.scheme}://{u.netloc}"
    remote = u.hostname not in LOCAL_HOSTS and not u.hostname.startswith("127.")
    path = u.path.rstrip("/")
    for suffix, hint in KNOWN_SUFFIXES:
        if path.endswith(suffix):
            return Target(origin + path[: -len(suffix)], None, hint, remote)
    return Target(origin, path or None, None, remote)


def base_url_for(target: str) -> str:
    return parse_target(target).url


def _input_field(op: dict[str, Any], spec: dict[str, Any]) -> str:
    """Pick the request field from an OpenAPI operation's JSON body schema."""
    try:
        schema = op["requestBody"]["content"]["application/json"]["schema"]
        if "$ref" in schema:
            schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
        props = list(schema.get("properties", {}))
    except (KeyError, TypeError):
        return "prompt"
    return next((f for f in INPUT_FIELDS if f in props), props[0] if len(props) == 1 else "prompt")


AUTH_HINT = ("the server requires authentication. Put the key in an environment variable and pass its name: "
             "/custom-model NAME URL --api-key-env MY_KEY_VAR")


def _check_auth(r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise ValueError(f"{r.request.url} answered {r.status_code}: {AUTH_HINT}")


async def detect(target: str, path: str | None = None, timeout: float = 8.0,
                 headers: dict[str, str] | None = None) -> Detected:
    t = parse_target(target)
    async with httpx.AsyncClient(timeout=timeout, headers=headers or {}, follow_redirects=True) as c:
        try:
            await c.get(t.url + "/" if not t.path else t.url)
        except httpx.HTTPError as e:
            where = "on that port" if not t.remote else "at that address"
            raise ValueError(f"nothing is answering at {t.url} ({e.__class__.__name__}). Is your model server "
                             f"running {where}?") from e
        if path:   # explicit endpoint path given as an extra argument
            if re.search(r"(/v1)?/chat/completions$|/v1$", path):
                return await _openai(c, t.base + (t.path or ""), [])
            return await _endpoint(c, t.url, "/" + path.lstrip("/"))
        if t.path:  # https://server/something: a prefix (proxy) in front of an API, or the endpoint itself
            d = await _detect_at(c, t.url, t.hint, probe=False)
            return d or await _endpoint(c, t.base, t.path)
        d = await _detect_at(c, t.base, t.hint, probe=True)
        if d:
            return d
    raise ValueError(f"the server at {t.url} is running, but it doesn't look like a model API Naga knows "
                     "(no /v1/models, /api/tags or a generate-style POST route). Pass the endpoint: "
                     "/custom-model <name> <port or URL> /your-endpoint, or give the full endpoint URL")


async def _detect_at(c: httpx.AsyncClient, base: str, hint: str | None, probe: bool) -> Detected | None:
    if hint != "ollama":
        try:
            r = await c.get(base + "/v1/models")
            _check_auth(r)
            data = r.json().get("data") if r.status_code == 200 else None
            if isinstance(data, list):
                ctx = next((m.get("max_model_len") or m.get("context_length") for m in data
                            if isinstance(m, dict) and (m.get("max_model_len") or m.get("context_length"))), None)
                d = await _openai(c, base, [m["id"] for m in data if isinstance(m, dict) and "id" in m])
                d.context_length = ctx
                return d
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            if isinstance(e, ValueError) and AUTH_HINT in str(e):
                raise
    if hint != "openai":
        try:
            r = await c.get(base + "/api/tags")
            _check_auth(r)
            if r.status_code == 200 and isinstance(r.json().get("models"), list):
                return Detected("ollama", base, [m["name"] for m in r.json()["models"]], native_tools=True)
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            if isinstance(e, ValueError) and AUTH_HINT in str(e):
                raise
    if hint == "openai":
        d = await _openai(c, base, [])
        d.notes.append("the server did not list its models; the model name is up to you (--model ID)")
        return d
    if hint == "ollama":
        return Detected("ollama", base, native_tools=True, notes=["could not list models (/api/tags)"])
    spec = await _openapi(c, base)
    paths: dict[str, Any] = (spec or {}).get("paths", {})
    if "/v1/chat/completions" in paths:
        d = await _openai(c, base, [])
        d.notes.append("the server has no /v1/models; the model name is up to you (--model ID)")
        return d
    posts = [(p, ops["post"]) for p, ops in paths.items() if isinstance(ops, dict) and "post" in ops and GEN_PATH.search(p)]
    if posts:
        p, op = sorted(posts, key=lambda x: (not re.search(r"generat|chat|complet", x[0], re.I), len(x[0])))[0]
        d = Detected("simple_http", base, path=p, input_field=_input_field(op, spec or {}))
        if len(posts) > 1:
            d.notes.append(f"several endpoints found ({', '.join(x[0] for x in posts[:5])}); using {p}")
        return d
    return await _probe_paths(c, base, COMMON_PATHS) if probe else None


async def _openai(c: httpx.AsyncClient, base: str, models: list[str]) -> Detected:
    d = Detected("openai_compatible", base, models)
    d.native_tools = await _probe_tools(c, base, models[0] if models else "default")
    return d


async def _endpoint(c: httpx.AsyncClient, base: str, path: str) -> Detected:
    """A specific endpoint: learn its request field from /openapi.json or by trying common field names."""
    spec = await _openapi(c, base)
    op = ((spec or {}).get("paths", {}).get(path) or {}).get("post")
    if op:
        return Detected("simple_http", base, path=path, input_field=_input_field(op, spec or {}))
    found = await _probe_paths(c, base, (path,), require=False)
    return found or Detected("simple_http", base, path=path, notes=["could not confirm the request field; using 'prompt'"])


COMMON_PATHS = ("/generate", "/api/generate", "/chat", "/api/chat", "/completion", "/completions", "/predict",
                "/infer", "/inference", "/ask", "/query", "/v1/generate", "/run")


async def _probe_paths(c: httpx.AsyncClient, base: str, paths: tuple[str, ...], require: bool = True) -> Detected | None:
    """For servers without /openapi.json (Flask, http.server, ...): find a generate-style POST route.

    A 405 on GET marks a POST-only route without generating anything; otherwise a tiny test prompt is POSTed
    and the route is accepted only if its reply contains text Naga can extract."""
    candidates: list[str] = []
    for p in paths:
        try:
            r = await c.get(base + p)
        except httpx.HTTPError:
            continue
        if r.status_code == 405:
            candidates.insert(0, p)
        elif r.status_code != 404:
            candidates.append(p)
    for p in candidates or list(paths):
        for fld in ("prompt", "message", "messages", "text", "input"):
            payload: Any = [{"role": "user", "content": "Reply with OK"}] if fld == "messages" else "Reply with OK"
            try:
                r = await c.post(base + p, json={fld: payload, "max_tokens": 8, "max_new_tokens": 8}, timeout=120)
            except httpx.HTTPError:
                break
            _check_auth(r)
            if r.status_code == 404:
                break
            if r.status_code < 400:
                try:
                    text = extract_text(r.json())
                except ValueError:
                    text = r.text
                if text:
                    return Detected("simple_http", base, path=p, input_field=fld,
                                    notes=[f"found POST {p} by probing (field '{fld}')"])
    return None


async def _openapi(c: httpx.AsyncClient, base: str) -> dict[str, Any] | None:
    try:
        r = await c.get(base + "/openapi.json")
        return r.json() if r.status_code == 200 and isinstance(r.json(), dict) else None
    except (httpx.HTTPError, ValueError):
        return None


async def _probe_tools(c: httpx.AsyncClient, base: str, model: str) -> bool:
    """Does /v1/chat/completions accept a `tools` list? (1-token request; errors mean 'use text tools')."""
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "noop", "description": "no-op",
                                                        "parameters": {"type": "object", "properties": {}}}}]}
    try:
        r = await c.post(base + "/v1/chat/completions", json=body, timeout=60)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def save(name: str, d: Detected, model: str, num_ctx: int | None = None, api_key_env: str | None = None) -> None:
    """Write provider custom-<name> and profile <name> to the user config."""
    validate_name(name)
    prov = f"providers.custom-{name}"
    unset_user_value(prov)
    set_user_value(f"{prov}.kind", f'"{d.kind}"')
    set_user_value(f"{prov}.base_url", f'"{d.base_url}"')
    set_user_value(f"{prov}.enabled", "true")
    if api_key_env:
        set_user_value(f"{prov}.api_key_env", f'"{api_key_env}"')
    if d.kind == "simple_http":
        set_user_value(f"{prov}.path", f'"{d.path}"')
        set_user_value(f"{prov}.input_field", f'"{d.input_field}"')
        set_user_value(f"{prov}.model", f'"{model}"')
    prof = f"profiles.{name}"
    unset_user_value(prof)
    set_user_value(f"{prof}.provider", f'"custom-{name}"')
    set_user_value(f"{prof}.model", f'"{model}"')
    set_user_value(f"{prof}.num_ctx", str(num_ctx or d.context_length or 8192))
    set_user_value(f"{prof}.temperature", "0.2")
    set_user_value(f"{prof}.tool_mode", '"native"' if d.native_tools else '"text"')
    set_user_value(f"{prof}.custom", "true")


def apply(config: Config, name: str, d: Detected, model: str, num_ctx: int | None = None,
          api_key_env: str | None = None) -> None:
    """Make the same change in an already-loaded config (so the running session can switch immediately)."""
    spec: dict[str, Any] = {"kind": d.kind, "base_url": d.base_url, "enabled": True}
    if api_key_env:
        spec["api_key_env"] = api_key_env
    if d.kind == "simple_http":
        spec.update(path=d.path, input_field=d.input_field, model=model)
    config.data.setdefault("providers", {})[f"custom-{name}"] = spec
    config.data.setdefault("profiles", {})[name] = {
        "provider": f"custom-{name}", "model": model, "num_ctx": num_ctx or d.context_length or 8192,
        "temperature": 0.2, "tool_mode": "native" if d.native_tools else "text", "custom": True}


def validate_name(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError("name may only use letters, digits, '.', '_' and '-'")
    if name in BUILTIN_PROFILES:
        raise ValueError(f"'{name}' is a built-in profile name; pick another")


def custom_profiles(config: Config) -> dict[str, dict[str, Any]]:
    return {n: p for n, p in config.section("profiles").items() if p.get("custom")}


def remove(config: Config, name: str) -> None:
    if name not in custom_profiles(config):
        raise ValueError(f"no custom model '{name}'")
    unset_user_value(f"profiles.{name}")
    unset_user_value(f"providers.custom-{name}")
    saved = user_config_path()
    saved_default = (tomllib.loads(saved.read_text()).get("general", {}).get("default_profile")
                     if saved.is_file() else None)
    if name in {saved_default, config.get("general.default_profile")}:
        unset_user_value("general.default_profile")
        config.data.setdefault("general", {})["default_profile"] = "coding"
    config.data["profiles"].pop(name, None)
    config.data.get("providers", {}).pop(f"custom-{name}", None)
