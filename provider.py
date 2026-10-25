"""GitHub Copilot, as a model provider for Flash.

Flash runs this once per request, with JSON on stdin (see Flash's
docs/EXTENSIONS.md, Providers). Flash's models are named @ghc/<model>,
such as @ghc/gpt-4.1: the id is Copilot's own.

Flash sends Ollama's shapes; this turns them into what the Copilot API
speaks and the answer back. Copilot serves each model on one of two
endpoints, /chat/completions or /responses, and this speaks both, picking
the one the model's listing names. A reply is replayed to the model as it
wrote it (reasoning items and tool call ids included) from a cache kept in
this extension's folder: Flash keeps only the text and the tool calls of a
reply, and the API wants the rest back on the next request.

Only the standard library is used, so nothing is installed.

The token is this extension's own setting (API key, on Flash's Extensions
page, or /extension set ghc api_key <token>), handed to it as
FLASH_SETTING_API_KEY. Signing in, tried in this order: that, then
COPILOT_GITHUB_TOKEN, GH_TOKEN, GITHUB_TOKEN, then the token
`gh auth token` gives, if you have signed in with `gh`.

Settings, from the environment (/set NAME VALUE in Flash):
  COPILOT_GITHUB_TOKEN  a GitHub token that can use Copilot, unless the
                        setting has one
  COPILOT_EFFORT        low, medium, high, xhigh or max; a reply Flash asks
                        to think harder is always the model's highest
  COPILOT_ALL_MODELS    1 to list every chat model Copilot names, not only
                        those it puts forward
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 -- `gh auth token`
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

HERE = Path(os.environ.get("FLASH_EXTENSION_DIR") or Path(__file__).parent)
CACHE = HERE / ".cache"
REPLIES = HERE / ".replies"
KEEP_REPLIES = 2000

DEFAULT_API = "https://api.githubcopilot.com"
USER_URL = "https://api.github.com/copilot_internal/user"

# What Copilot checks of a client is its integration id.
HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "flash/1.0",
    "Editor-Plugin-Version": "flash-copilot/0.1.0",
    "User-Agent": "flash-copilot/0.1.0",
    "Openai-Intent": "conversation-agent",
}

TIMEOUT = 300
EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
MODELS_TTL = 600
USER_TTL = 86400

NO_TOKEN = (
    "No GitHub token that can use Copilot was found. Give it one: in its "
    "settings on Flash's Extensions page, with /extension set ghc api_key "
    "<token>, or /set COPILOT_GITHUB_TOKEN <token>. Or sign in with "
    "`gh auth login`."
)

# A reply Flash relays as a note to the model rather than its own turn.
NOTE = "[A note from Flash]"
NOT_RUN = "The tool was not run."


def say(answer: dict) -> None:
    print(json.dumps(answer), flush=True)


class ApiError(Exception):
    """A failed request, with what the API said and its HTTP status."""

    def __init__(self, status: int, message: str, code: str = "",
                 retry_after: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after


# Settings --------------------------------------------------------------------


def api_key() -> str:
    """The token from this extension's settings, which goes ahead of the
    environment's; empty to leave it to them."""

    return os.environ.get("FLASH_SETTING_API_KEY", "").strip()


def setting(name: str) -> str:
    """COPILOT_<NAME> from the environment, which is where /set in Flash
    puts it."""

    return os.environ.get(f"COPILOT_{name.upper()}", "").strip()


# Signing in ------------------------------------------------------------------


def _gh() -> str | None:
    """The token `gh` holds, when it is installed and signed in."""

    program = shutil.which("gh") or next(
        (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh",
                     "/usr/bin/gh") if os.access(p, os.X_OK)), None,
    )
    if not program:
        return None
    try:
        done = subprocess.run(  # nosec B603
            [program, "auth", "token"], capture_output=True, text=True,
            timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def candidates() -> Iterator[tuple[str, str]]:
    """Every token there might be, as (where from, token), best first.
    Classic personal access tokens (ghp_) cannot use Copilot."""

    sources = [
        ("the API key setting", api_key),
        ("COPILOT_GITHUB_TOKEN", lambda: os.environ.get("COPILOT_GITHUB_TOKEN")),
        ("GH_TOKEN", lambda: os.environ.get("GH_TOKEN")),
        ("GITHUB_TOKEN", lambda: os.environ.get("GITHUB_TOKEN")),
        ("gh auth token", _gh),
    ]
    seen: set[str] = set()
    for where, get in sources:
        token = (get() or "").strip()
        if token and token not in seen and not token.startswith("ghp_"):
            seen.add(token)
            yield where, token


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


# HTTP ------------------------------------------------------------------------


def _raise(exc: urllib.error.HTTPError) -> None:
    raw = exc.read().decode("utf-8", "replace")
    message, code = raw.strip()[:500], ""
    try:
        found = json.loads(raw)
        error = found.get("error", found)
        if isinstance(error, dict):
            message = error.get("message") or message
            code = str(error.get("code") or "")
        elif isinstance(error, str):
            message = error
    except ValueError:
        pass
    raise ApiError(exc.code, message or exc.reason, code,
                   exc.headers.get("retry-after", "")) from None


def http(url: str, token: str, body: dict | None = None, *,
         scheme: str = "Bearer", headers: dict | None = None):
    """Open URL as TOKEN. The open response, for a caller to read."""

    head = {**HEADERS, "Authorization": f"{scheme} {token}",
            "Accept": "application/json", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        head["Content-Type"] = "application/json"
    last: Exception | None = None
    for attempt in range(2):
        try:
            return urllib.request.urlopen(  # nosec B310 -- https only
                urllib.request.Request(url, data=data, headers=head),
                timeout=TIMEOUT,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (502, 503, 504) and attempt == 0:
                time.sleep(1.5)
                continue
            _raise(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt == 0:
                time.sleep(1.5)
    raise ApiError(503, "Could not reach GitHub Copilot. Check the "
                        f"connection ({getattr(last, 'reason', last)}).")


def get_json(url: str, token: str, **kwargs) -> Any:
    with http(url, token, **kwargs) as response:
        return json.load(response)


def _lookup(token: str) -> str | None:
    """The API this token's account is served by, or None when GitHub says
    it is no good for Copilot. Kept for a day."""

    path = CACHE / "user.json"
    try:
        kept = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        kept = {}
    found = kept.get(_digest(token))
    if found and time.time() - found["at"] < USER_TTL:
        return found["api"]
    try:
        user = get_json(USER_URL, token, scheme="token")
    except ApiError as exc:
        if exc.status in (401, 403, 404):
            return None
        return DEFAULT_API  # GitHub is having a moment: try the default
    api = ((user.get("endpoints") or {}).get("api") or DEFAULT_API).rstrip("/")
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        kept[_digest(token)] = {"api": api, "at": time.time()}
        path.write_text(json.dumps(kept), encoding="utf-8")
    except OSError:
        pass
    return api


def _forget_lookups() -> None:
    (CACHE / "user.json").unlink(missing_ok=True)


def sign_in() -> tuple[str, str, str]:
    """(where the token came from, the token, the API to ask), for the
    first token GitHub accepts for Copilot."""

    tried = []
    for where, token in candidates():
        api = _lookup(token)
        if api:
            return where, token, api
        tried.append(where)
    if tried:
        raise ApiError(401, "GitHub did not accept the token from "
                       + ", ".join(tried) + " for Copilot. Check that the "
                       "account has Copilot.", "bad_token")
    raise ApiError(401, NO_TOKEN, "no_token")


# Models ----------------------------------------------------------------------


def listing(token: str, api: str) -> list[dict]:
    """The models Copilot names for this account, kept for ten minutes."""

    path = CACHE / f"models-{_digest(token)}.json"
    try:
        if time.time() - path.stat().st_mtime < MODELS_TTL:
            found = json.loads(path.read_text("utf-8"))
            if isinstance(found, list):
                return found
    except (OSError, ValueError):
        pass
    found = get_json(f"{api}/models", token).get("data") or []
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found), encoding="utf-8")
    except OSError:
        pass
    return found


def _supports(model: dict) -> dict:
    return (model.get("capabilities") or {}).get("supports") or {}


def _limits(model: dict) -> dict:
    return (model.get("capabilities") or {}).get("limits") or {}


def offered(model: dict, everything: bool = False) -> bool:
    """Whether MODEL is one to list: a chat model the account may use and
    Copilot puts forward, not an embedding model or one switched off."""

    if (model.get("capabilities") or {}).get("type") != "chat":
        return False
    state = (model.get("policy") or {}).get("state")
    if state == "disabled":
        return False
    if everything:
        return True
    if re.search(r"-\d{4}-\d{2}-\d{2}$", model.get("id") or ""):
        return False  # gpt-4.1-2025-04-14 is gpt-4.1, dated
    return bool(state == "enabled" or model.get("model_picker_enabled")
                or model.get("model_picker_category"))


def efforts_of(model: dict) -> list[str]:
    found = _supports(model).get("reasoning_effort")
    return [e for e in found if e in EFFORTS] if isinstance(found, list) \
        else []


def thinks(model: dict) -> bool:
    supports = _supports(model)
    return bool(efforts_of(model) or supports.get("adaptive_thinking")
                or supports.get("min_thinking_budget"))


def capabilities_of(model: dict) -> list[str]:
    supports = _supports(model)
    found = ["completion"]
    if supports.get("tool_calls"):
        found.append("tools")
    if supports.get("vision"):
        found.append("vision")
    if thinks(model):
        found.append("thinking")
    return found


def wires_of(model: dict | None) -> list[str]:
    """The ways to ask MODEL, best first: "chat" is /chat/completions,
    "responses" is /responses. A model that names none is an older one,
    asked the first way."""

    named = (model or {}).get("supported_endpoints") or []
    found = [w for w, path in (("chat", "/chat/completions"),
                               ("responses", "/responses"))
             if path in named]
    return found or ["chat"]


def models() -> None:
    try:
        _, token, api = sign_in()
        found = listing(token, api)
    except ApiError as exc:
        say({"error": exc.message, "status": exc.status})
        return
    everything = setting("all_models") == "1"
    listed = []
    for model in found:
        if not offered(model, everything) or not model.get("id"):
            continue
        entry: dict[str, Any] = {"name": model["id"],
                                 "capabilities": capabilities_of(model)}
        context = _limits(model).get("max_context_window_tokens")
        if context:
            entry["context"] = context
        listed.append(entry)
    say({"models": listed})


def find(found: list[dict], name: str) -> dict | None:
    return next((m for m in found if m.get("id") == name), None)


# Effort ----------------------------------------------------------------------


def effort_for(ask: dict, model: dict | None) -> str | None:
    """How hard MODEL should think, held to the levels the model has. A
    reply Flash asks to think harder (Think harder, or Flash deciding a
    message needs it) goes as hard as the model does; else the effort
    setting; else None, which leaves it to the model."""

    have = efforts_of(model or {})
    if not have:
        return None
    think = ask.get("think")
    wanted = setting("effort").lower()
    if isinstance(think, str) and think.lower() in EFFORTS:
        wanted = think.lower()
    elif think is True or (isinstance(think, str) and think):
        wanted = "max"
    elif wanted not in EFFORTS:
        if think is not False:
            return None
        wanted = "none"
    if wanted in have:
        return wanted
    # The nearest level it has: "max" on a model that tops out at "high".
    rank = EFFORTS.index(wanted)
    return min(have, key=lambda e: abs(EFFORTS.index(e) - rank))


# Replies kept for replay -----------------------------------------------------


def _calls_of(message: dict) -> list[dict]:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        calls.append({"name": function.get("name", ""),
                      "arguments": arguments})
    return calls


def _key(content: str, calls: list) -> str:
    """A reply, known by the text and tool calls Flash keeps of it."""

    shape = {
        "content": (content or "").strip(),
        "calls": [[c.get("name", ""), c.get("arguments") or {}]
                  for c in calls],
    }
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True).encode("utf-8")
    ).hexdigest()


def keep(content: str, calls: list, wire: str, raw: Any) -> None:
    """Keep RAW, the reply as the API gave it, for its replay."""

    try:
        REPLIES.mkdir(parents=True, exist_ok=True)
        (REPLIES / f"{_key(content, calls)}.json").write_text(
            json.dumps({"wire": wire, "raw": raw}), encoding="utf-8",
        )
        old = sorted(REPLIES.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in old[:-KEEP_REPLIES]:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def kept(content: str, calls: list, wire: str) -> Any:
    try:
        found = json.loads(
            (REPLIES / f"{_key(content, calls)}.json").read_text("utf-8")
        )
    except (OSError, ValueError):
        return None
    return found.get("raw") if found.get("wire") == wire else None


# Flash's messages as the API's -----------------------------------------------


def _media_type(data: str) -> str:
    head = base64.b64decode(data[:32] + "=" * (-len(data[:32]) % 4))
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _image_urls(message: dict) -> list[str]:
    return [f"data:{_media_type(data)};base64,{data}"
            for data in message.get("images") or []]


def _text(message: dict) -> str:
    text = message.get("content") or ""
    return text if isinstance(text, str) else json.dumps(text)


def _json(arguments: Any) -> str:
    return json.dumps(arguments if arguments is not None else {})


class Pending:
    """Tool calls made and not yet answered. Flash's tool messages carry no
    id, only the tool's name: each answers the first open call of that name."""

    def __init__(self) -> None:
        self.open: list[tuple[str, str]] = []

    def add(self, call_id: str, name: str) -> None:
        self.open.append((call_id, name))

    def take(self, name: str) -> str | None:
        for i, (call_id, called) in enumerate(self.open):
            if called == name:
                return self.open.pop(i)[0]
        return self.open.pop(0)[0] if self.open else None

    def drain(self) -> list[str]:
        ids = [call_id for call_id, _ in self.open]
        self.open.clear()
        return ids


def chat_messages(messages: list) -> list[dict]:
    """MESSAGES, in Ollama's shape, as /chat/completions takes them."""

    out: list[dict] = []
    pending = Pending()

    def settle() -> None:
        for call_id in pending.drain():
            out.append({"role": "tool", "tool_call_id": call_id,
                        "content": NOT_RUN})

    for index, message in enumerate(messages):
        role, text = message.get("role"), _text(message)

        if role == "system":
            settle()
            out.append({"role": "system", "content": text} if not out
                       else {"role": "user", "content": f"{NOTE}\n{text}"})
        elif role == "tool":
            name = message.get("tool_name") or "the tool"
            call_id = pending.take(name)
            if call_id is None:
                out.append({"role": "user",
                            "content": f"[{name} returned]\n{text}"})
            else:
                out.append({"role": "tool", "tool_call_id": call_id,
                            "content": text or "(no output)"})
        elif role == "assistant":
            settle()
            calls = _calls_of(message)
            reply = kept(text, calls, "chat")
            if reply is None:
                reply = {"role": "assistant", "content": text or None}
                if calls:
                    reply["tool_calls"] = [{
                        "id": f"call_flash_{index}_{j}", "type": "function",
                        "function": {"name": c["name"],
                                     "arguments": _json(c["arguments"])},
                    } for j, c in enumerate(calls)]
            if not reply.get("content") and not reply.get("tool_calls"):
                continue
            out.append(reply)
            for call in reply.get("tool_calls") or []:
                pending.add(call["id"], call["function"]["name"])
        else:
            settle()
            pictures = _image_urls(message)
            if pictures:
                parts: list[dict] = [
                    {"type": "image_url", "image_url": {"url": url}}
                    for url in pictures
                ]
                if text.strip():
                    parts.append({"type": "text", "text": text})
                out.append({"role": "user", "content": parts})
            elif text.strip():
                out.append({"role": "user", "content": text})
    settle()
    return out


def responses_input(messages: list) -> tuple[str, list[dict]]:
    """MESSAGES as /responses takes them: its instructions and its items."""

    instructions: list[str] = []
    items: list[dict] = []
    pending = Pending()

    def settle() -> None:
        for call_id in pending.drain():
            items.append({"type": "function_call_output",
                          "call_id": call_id, "output": NOT_RUN})

    for index, message in enumerate(messages):
        role, text = message.get("role"), _text(message)

        if role == "system":
            if not items:
                instructions.append(text)
            else:
                settle()
                items.append({"role": "user", "content": [
                    {"type": "input_text", "text": f"{NOTE}\n{text}"}]})
        elif role == "tool":
            name = message.get("tool_name") or "the tool"
            call_id = pending.take(name)
            if call_id is None:
                items.append({"role": "user", "content": [
                    {"type": "input_text",
                     "text": f"[{name} returned]\n{text}"}]})
            else:
                items.append({"type": "function_call_output",
                              "call_id": call_id,
                              "output": text or "(no output)"})
        elif role == "assistant":
            settle()
            calls = _calls_of(message)
            raw = kept(text, calls, "responses")
            if raw is None:
                raw = []
                if text.strip():
                    raw.append({"role": "assistant", "content": [
                        {"type": "output_text", "text": text}]})
                for j, call in enumerate(calls):
                    raw.append({
                        "type": "function_call",
                        "call_id": f"call_flash_{index}_{j}",
                        "name": call["name"],
                        "arguments": _json(call["arguments"]),
                    })
            items.extend(raw)
            for item in raw:
                if item.get("type") == "function_call":
                    pending.add(item["call_id"], item["name"])
        else:
            settle()
            parts = [{"type": "input_image", "image_url": url}
                     for url in _image_urls(message)]
            if text.strip():
                parts.append({"type": "input_text", "text": text})
            if parts:
                items.append({"role": "user", "content": parts})
    settle()
    return "\n\n".join(s for s in instructions if s.strip()), items


def tools_of(tools: list | None, wire: str) -> list[dict]:
    """Ollama's tool schemas as the API's."""

    found = []
    for tool in tools or []:
        function = tool.get("function") or tool
        if not function.get("name"):
            continue
        schema = function.get("parameters") or {"type": "object",
                                                "properties": {}}
        if wire == "chat":
            found.append({"type": "function", "function": {
                "name": function["name"],
                "description": function.get("description") or "",
                "parameters": schema}})
        else:
            # Not strict: Flash's schemas are not the closed ones strict
            # mode demands.
            found.append({"type": "function", "name": function["name"],
                          "description": function.get("description") or "",
                          "parameters": schema, "strict": False})
    return found


# The API's answer as Flash's -------------------------------------------------


def sse(response) -> Iterator[dict]:
    """The JSON of each event in a server-sent stream."""

    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


class Out:
    """What Flash is told. With STREAM, the words and thinking go as they
    come, and the last line carries what is left: the tool calls and
    counts. Without it, one line carries everything."""

    def __init__(self, stream: bool) -> None:
        self.stream = stream
        self.text: list[str] = []
        self.thinking: list[str] = []

    def words(self, piece: str) -> None:
        if piece:
            self.text.append(piece)
            if self.stream:
                say({"message": {"content": piece}, "done": False})

    def thought(self, piece: str) -> None:
        if piece:
            self.thinking.append(piece)
            if self.stream:
                say({"message": {"thinking": piece}, "done": False})

    def done(self, calls: list[dict], prompt: int, output: int,
             reason: str) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        if not self.stream:
            message["content"] = "".join(self.text)
            if self.thinking:
                message["thinking"] = "".join(self.thinking)
        if calls:
            message["tool_calls"] = [{"function": c} for c in calls]
        say({"message": message, "prompt_eval_count": prompt,
             "eval_count": output, "done": True, "done_reason": reason})


CUT_OFF = ("\n\n(The reply ran out of room before its tool call was "
           "complete.)")


def _arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        found = json.loads(raw or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def run_chat(ask: dict, token: str, api: str, model: dict | None,
             out: Out) -> None:
    """One request to /chat/completions."""

    name = ask["model"]
    body: dict[str, Any] = {
        "model": name,
        "messages": chat_messages(ask.get("messages") or []),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    tools = tools_of(ask.get("tools"), "chat")
    if tools:
        body["tools"] = tools
    effort = effort_for(ask, model)
    if effort:
        body["reasoning_effort"] = effort
    temperature = (ask.get("options") or {}).get("temperature")
    if isinstance(temperature, (int, float)) and not thinks(model or {}):
        body["temperature"] = temperature
    if (model or {}).get("vendor") == "Anthropic":
        # Claude's API will not answer without a limit.
        body["max_tokens"] = min(_limits(model or {}).get(
            "max_output_tokens") or 16384, 32000)

    text: list[str] = []
    reasoning: dict[str, str] = {}
    calls: dict[int, dict] = {}
    finish, usage = "", {}

    with http(f"{api}/chat/completions", token, body) as response:
        for event in sse(response):
            if event.get("error"):
                error = event["error"]
                raise ApiError(500, (error.get("message") if isinstance(
                    error, dict) else str(error)) or "The stream failed.")
            usage = event.get("usage") or usage
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text.append(delta["content"])
                    out.words(delta["content"])
                thought = delta.get("reasoning_text") \
                    or delta.get("reasoning_content")
                if thought:
                    out.thought(thought)
                for field in ("reasoning_text", "reasoning_content",
                              "reasoning_opaque"):
                    if delta.get(field):
                        reasoning[field] = reasoning.get(field, "") \
                            + delta[field]
                for piece in delta.get("tool_calls") or []:
                    call = calls.setdefault(piece.get("index") or 0, {
                        "id": "", "name": "", "arguments": ""})
                    call["id"] = piece.get("id") or call["id"]
                    function = piece.get("function") or {}
                    call["name"] += function.get("name") or ""
                    call["arguments"] += function.get("arguments") or ""
                finish = choice.get("finish_reason") or finish

    content = "".join(text)
    made = [calls[i] for i in sorted(calls) if calls[i]["name"]]
    for j, call in enumerate(made):
        call["id"] = call["id"] or f"call_flash_{j}"
    flash_calls = [{"name": c["name"], "arguments": _arguments(c["arguments"])}
                   for c in made]

    if finish == "length" and flash_calls:
        # A tool call cut off mid-input is never run.
        flash_calls, made = [], []
        out.words(CUT_OFF)
        content += CUT_OFF

    raw: dict[str, Any] = {"role": "assistant", "content": content or None,
                           **reasoning}
    if made:
        raw["tool_calls"] = [{
            "id": c["id"], "type": "function",
            "function": {"name": c["name"], "arguments": c["arguments"]},
        } for c in made]
    keep(content, flash_calls, "chat", raw)
    out.done(flash_calls, usage.get("prompt_tokens") or 0,
             usage.get("completion_tokens") or 0, finish or "stop")


def _item_text(item: dict) -> str:
    return "".join(part.get("text", "") for part in item.get("content") or []
                   if part.get("type") == "output_text")


def run_responses(ask: dict, token: str, api: str, model: dict | None,
                  out: Out) -> None:
    """One request to /responses."""

    instructions, items = responses_input(ask.get("messages") or [])
    body: dict[str, Any] = {
        "model": ask["model"],
        "input": items,
        "stream": True,
        # Nothing is kept on the other side: the reasoning comes back
        # encrypted, in the reply, to be replayed.
        "store": False,
    }
    if instructions:
        body["instructions"] = instructions
    tools = tools_of(ask.get("tools"), "responses")
    if tools:
        body["tools"] = tools
    if thinks(model or {}):
        reasoning: dict[str, str] = {"summary": "auto"}
        effort = effort_for(ask, model)
        if effort:
            reasoning["effort"] = effort
        body["reasoning"] = reasoning
        body["include"] = ["reasoning.encrypted_content"]

    done_items: list[dict] = []
    final: dict = {}

    with http(f"{api}/responses", token, body) as response:
        for event in sse(response):
            kind = event.get("type", "")
            if kind == "response.output_text.delta":
                out.words(event.get("delta") or "")
            elif kind in ("response.reasoning_summary_text.delta",
                          "response.reasoning_text.delta"):
                out.thought(event.get("delta") or "")
            elif kind == "response.reasoning_summary_part.done":
                out.thought("\n\n")
            elif kind == "response.output_item.done":
                done_items.append(event.get("item") or {})
            elif kind in ("response.completed", "response.incomplete"):
                final = event.get("response") or {}
            elif kind == "response.failed":
                error = (event.get("response") or {}).get("error") or {}
                raise ApiError(500, error.get("message") or "The reply failed.",
                               str(error.get("code") or ""))
            elif kind == "error":
                raise ApiError(500, event.get("message") or "The stream failed.",
                               str(event.get("code") or ""))

    raw = done_items or final.get("output") or []
    content = "".join(out.text)
    if not content:
        content = "".join(_item_text(i) for i in raw
                          if i.get("type") == "message")
        out.words(content)
    flash_calls = [
        {"name": i["name"], "arguments": _arguments(i.get("arguments"))}
        for i in raw if i.get("type") == "function_call"
    ]

    reason = "stop"
    if final.get("status") == "incomplete":
        reason = "length"
        if flash_calls:
            # A tool call cut off mid-input is never run.
            flash_calls = []
            raw = [i for i in raw if i.get("type") != "function_call"]
            out.words(CUT_OFF)
            content += CUT_OFF
    elif flash_calls:
        reason = "tool_calls"

    keep(content, flash_calls, "responses", raw)
    usage = final.get("usage") or {}
    out.done(flash_calls, usage.get("input_tokens") or 0,
             usage.get("output_tokens") or 0, reason)


# The two requests ------------------------------------------------------------


def describe(exc: ApiError, model: str = "") -> dict:
    """What went wrong, said so the user knows what to do about it."""

    if exc.code in ("no_token", "bad_token"):
        message = exc.message
    elif exc.status == 401:
        message = "GitHub did not accept the token for Copilot. " + NO_TOKEN
    elif exc.code in ("model_not_supported", "model_not_found") \
            or exc.status == 404:
        message = (f"Copilot does not give this account {model or 'that model'}"
                   ": it may need another plan, or be switched off in the "
                   "account's Copilot settings. Pick another with /model.")
    elif exc.status == 429:
        message = "Copilot is rate limiting this account" + (
            f": try again in {exc.retry_after}s." if exc.retry_after else ".")
    else:
        message = exc.message
    return {"error": message, "status": exc.status}


def chat(ask: dict) -> None:
    out = Out(bool(ask.get("stream")))
    runners = {"chat": run_chat, "responses": run_responses}
    tried_in = False  # signed in again after a 401

    try:
        _, token, api = sign_in()
        try:
            model = find(listing(token, api), ask["model"])
        except ApiError:
            model = None
        order = wires_of(model)
        order += [w for w in runners if w not in order]
        position = 0
        while position < len(order):
            try:
                runners[order[position]](ask, token, api, model, out)
                return
            except ApiError as exc:
                if exc.status == 401 and not tried_in:
                    tried_in = True
                    _forget_lookups()
                    _, token, api = sign_in()
                elif exc.code == "unsupported_api_for_model" \
                        and position + 1 < len(order):
                    # Asked the wrong way: the model's other endpoint.
                    position += 1
                else:
                    raise
    except ApiError as exc:
        say(describe(exc, ask.get("model", "")))


def main() -> None:
    ask = json.load(sys.stdin)
    try:
        if ask.get("action") == "models":
            models()
        elif ask.get("action") == "chat":
            chat(ask)
        else:
            say({"error": f"unknown action {ask.get('action')!r}"})
    except Exception as exc:  # noqa: BLE001 -- Flash wants JSON, not a trace
        say({"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
