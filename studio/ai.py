"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    QLIPIT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    QLIPIT_AI_MODELS     one or more model ids, comma separated, tried in this order
    QLIPIT_AI_KEY        your key (or QLIPIT_AI_KEY_FILE, a file holding it)
    QLIPIT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import math
import os
import re
import threading
import time

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}
MAX_PROMPT_CHARS, MAX_OUTPUT_CHARS, MAX_OUTPUT_TOKENS = 60000, 65536, 8192
# A timeout does not kill a Python thread or a remote request. Its slot stays occupied until it really exits.
_capacity = max(1, config.JOB_WORKERS)
_slots = threading.BoundedSemaphore(min(_capacity, max(1, int(config.env("QLIPIT_AI_CONCURRENCY", str(_capacity))))))
_request = threading.local()


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    snapshot = getattr(_request, "settings", None)
    if snapshot is not None:
        return snapshot
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    return {"provider": ALIASES.get(p, p),
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": str(d["key"]) if d.get("provider") and "key" in d else config.ai_key()}


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def key() -> str:
    return current()["key"]


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    base = (c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]).rstrip("/")
    if c["provider"] == "ollama" and base and not base.endswith("/v1"):
        base += "/v1"
    return base


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see. The key itself never leaves the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    c = current()
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "", "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None) -> dict:
    """Save the choice made in the dashboard. `new_key` None keeps the key that is already there (the page never
    has it to send back); an empty string removes it."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()
    k = old["key"] if new_key is None and old["provider"] == p else str(new_key or "").strip()[:400]
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"provider": p, "models": ms, "base_url": address, "key": k}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
        clear_problem()
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if p == "ollama" and base and not base.endswith("/v1"):
        base += "/v1"
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    headers = {"Authorization": f"Bearer {k}"} if k else {}
    if p == "openrouter":
        headers["HTTP-Referer"] = "https://qlipit.app"
        headers["X-Title"] = "QlipIt Studio"
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers=headers, timeout=20)
        if r.status_code != 200 and p == "ollama":
            native_url = base[:-3] if base.endswith("/v1") else base
            try:
                r2 = httpx.get(f"{native_url}/api/tags", timeout=20)
                if r2.status_code == 200:
                    d = r2.json()
                    return sorted({m.get("name", "") for m in d.get("models", []) if isinstance(m, dict) and m.get("name")})[:400]
            except Exception:
                pass
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and the model all work."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    out, sent, wrote = call(ms[0], 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
    clear_problem()
    return {"ok": True, "model": ms[0], "tokens": sent + wrote, "answer": str(out)[:80]}


class AIError(RuntimeError):
    pass


class AITimeoutError(AIError):
    pass


def recoverable(e):
    """Only transient transport/rate-limit/overload failures get another attempt on the same model."""
    return (isinstance(e, (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.TimeoutException, httpx.RemoteProtocolError))
            or bool(re.match(r"^HTTP (?:408|429|500|502|503|504)\b", str(e)))) and not isinstance(e, AITimeoutError)


def seconds():
    try:
        value = float(config.env("QLIPIT_AI_SECONDS", "300"))
    except (TypeError, ValueError) as e:
        raise AIError("QLIPIT_AI_SECONDS must be a positive finite number.") from e
    if not math.isfinite(value) or value <= 0:
        raise AIError("QLIPIT_AI_SECONDS must be a positive finite number.")
    return value


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again

# A prompt that does not fit the model's context window is not a missing capability: the caller has to send less.
_CONTEXT_ERROR = re.compile(
    r"context (?:length|window|size)|context_length|maximum context|"
    r"input (?:length|is too long)|too many tokens|prompt is too long|token limit", re.I)


def context_too_small(e) -> bool:
    """The provider rejected the prompt because it does not fit this model's context window."""
    return bool(_CONTEXT_ERROR.search(str(e)))


def explain(e) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    if context_too_small(e):
        return (f"{label()} model \"{name}\" could not fit this transcript in its context window. "
                f"Pick a model with a bigger context, or use a shorter video.")
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low:
        if provider() == "ollama":
            return "QlipIt couldn't reach Ollama. Check that Ollama is running (`ollama serve`)."
        if provider() == "custom":
            return f"QlipIt couldn't reach your custom AI service. Check that your local AI server (e.g. LM Studio, LocalAI) is running at {base_url()}."
        return f"QlipIt couldn't reach {label()}. Check the internet connection."
    if "malformed" in low or "schema" in low or "did not answer with json" in low:
        return f"{label()} model \"{name}\" returned output that didn't match the required format. Try another model."
    return ""


def note_problem(e):
    import time
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model):
    text = r.text[:300].replace(key() or "\0", "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def validate(out, schema, path="answer"):
    """Validate the complete small schema, including empty arrays (abstention), without coercing bad fields."""
    kind = str(schema.get("type", "")).lower()
    numeric = isinstance(out, (int, float)) and not isinstance(out, bool)
    try:
        finite = numeric and math.isfinite(float(out))
    except OverflowError:
        finite = False
    good = {"array": isinstance(out, list), "object": isinstance(out, dict), "string": isinstance(out, str),
            "number": finite,
            "integer": finite and float(out).is_integer(),
            "boolean": isinstance(out, bool), "null": out is None}
    if kind not in good or not good[kind]:
        raise AIError(f"The AI returned a malformed {path}: expected {kind or 'a schema type'}.")
    if kind == "array":
        if len(out) > 4096:
            raise AIError("The AI returned too many items.")
        for i, item in enumerate(out):
            validate(item, schema["items"], f"{path}[{i}]")
    elif kind == "object":
        if any(k not in out for k in schema.get("required", [])):
            raise AIError(f"The AI omitted required fields in {path}.")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(out) - set(props):
            raise AIError(f"The AI returned unexpected fields in {path}.")
        for k in out.keys() & props.keys():
            validate(out[k], props[k], f"{path}.{k}")
    if "enum" in schema and out not in schema["enum"]:
        raise AIError(f"The AI returned an unsupported value in {path}.")
    return out


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_OUTPUT_CHARS:
        raise AIError("The AI returned missing or oversized JSON.")
    text = text.strip()
    m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    candidate = m_fence.group(1).strip() if m_fence else None
    out = None
    if candidate:
        try:
            out = json.loads(candidate)
        except (ValueError, RecursionError):
            out = None
    if out is None:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
        try:
            out = json.loads(clean)
        except (ValueError, RecursionError):
            m = re.search(r"[\[{].*[\]}]", clean, re.S)
            if not m:
                raise AIError("The AI did not answer with JSON.")
            try:
                out = json.loads(m.group(0))
            except (ValueError, RecursionError) as e:
                raise AIError("The AI did not answer with valid JSON.") from e
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    if out is None:
        raise AIError("The AI returned null, not a valid empty answer.")
    return out


def _answer(text, schema, sent, wrote):
    sent, wrote = _tokens(sent), _tokens(wrote)
    try:
        return validate(_parse(text), schema), sent, wrote
    except AIError as e:
        e.tokens = (sent, wrote)
        raise


def _tokens(value):
    if value is None:
        return 0
    validate(value, {"type": "INTEGER"}, "token usage")
    if value < 0:
        raise AIError("The AI returned negative token usage.")
    return int(value)


def _envelope(data, how):
    """Provider completion status, text blocks and usage are part of correctness, not just the inner JSON."""
    if not isinstance(data, dict):
        raise AIError("The AI returned a malformed provider response.")
    fields = {"gemini": ("promptTokenCount", "totalTokenCount"), "openai": ("prompt_tokens", "completion_tokens"),
              "anthropic": ("input_tokens", "output_tokens")}[how]
    usage = data.get("usageMetadata" if how == "gemini" else "usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        raise AIError("The AI returned malformed token usage.")
    sent, wrote = (_tokens(usage.get(f)) for f in fields)
    if how == "gemini":
        if fields[1] in usage and wrote < sent:
            raise AIError("The AI's total token usage is smaller than its input usage.")
        wrote = max(0, wrote - sent)

    def malformed(message):
        error = AIError(message)
        error.tokens = (sent, wrote)  # known billed usage still belongs to the rejected answer
        raise error

    if how == "gemini":
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            malformed("The AI returned no completion candidate.")
        candidate = candidates[0]
        finished, allowed = candidate.get("finishReason"), (None, "STOP")
        content = candidate.get("content")
        blocks = content.get("parts") if isinstance(content, dict) else None
    elif how == "openai":
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            malformed("The AI returned no completion choice.")
        choice = choices[0]
        finished, allowed = choice.get("finish_reason"), (None, "stop")
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("refusal"):
            malformed("The AI returned no usable completion message.")
        blocks = [{"text": message.get("content")}]
    else:
        finished, allowed = data.get("stop_reason"), (None, "end_turn", "stop_sequence")
        blocks = data.get("content")
    if finished not in allowed:
        if str(finished).lower() in ("length", "max_tokens"):
            malformed("The AI ran out of output tokens before it finished its answer.")
        malformed("The AI's completion was truncated or refused, not a valid abstention.")
    if not isinstance(blocks, list) or not blocks or any(not isinstance(b, dict) for b in blocks):
        malformed("The AI returned malformed completion blocks.")
    texts = [b.get("text", "") for b in blocks
             if not b.get("thought") and (how != "anthropic" or b.get("type") == "text")]
    if any(not isinstance(text, str) for text in texts):
        malformed("The AI returned malformed completion text.")
    return "".join(texts), sent, wrote


def _timeout(ceiling):
    deadline = getattr(_request, "deadline", time.monotonic() + seconds())
    left = deadline - time.monotonic()
    if left <= 0 or (getattr(_request, "abandoned", None) and _request.abandoned.is_set()):
        raise AITimeoutError("The AI did not answer in time. Try a faster model.")
    return min(ceiling, left)


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict, *, deadline=None, settings=None):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back).
    A service that keeps the line open without answering (some send padding to hold the connection) is
    given up on after AI_SECONDS, so a video can never wait on it for ever."""
    if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_CHARS:
        raise AIError("The AI prompt exceeds the bounded context budget.")
    settings = dict(current() if settings is None else settings)
    if settings["provider"] not in PROVIDERS:
        raise AIError("The AI provider is not configured.")
    how = PROVIDERS[settings["provider"]][2]
    ask = {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how]
    box, secs = {}, seconds()
    end = min(time.monotonic() + secs, deadline) if deadline is not None else time.monotonic() + secs
    slots, abandoned = _slots, threading.Event()
    if not slots.acquire(timeout=max(0.0, end - time.monotonic())):
        raise AITimeoutError("AI capacity is still occupied by active or timed-out requests. Try again after they finish.")

    def work():
        _request.deadline, _request.abandoned, _request.settings = end, abandoned, settings
        try:
            out, sent, wrote = ask(model, prompt, schema)
            sent, wrote = _tokens(sent), _tokens(wrote)
            validate(out, schema)
            if not abandoned.is_set():
                box["out"] = out, sent, wrote
        except BaseException as e:  # noqa: BLE001 - handed back to the caller below
            if not abandoned.is_set():
                box["err"] = (AIError("The AI returned a malformed provider response.")
                              if isinstance(e, (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError)) else e)
        finally:
            _request.__dict__.clear()
            slots.release()

    t = threading.Thread(target=work, daemon=True, name="ai-request")
    try:
        t.start()
    except BaseException:
        slots.release()
        raise
    t.join(max(0.0, end - time.monotonic()))
    if t.is_alive():
        abandoned.set()
        raise AITimeoutError(f"The AI did not answer in time ({secs:g}s). Try a faster model.")
    if "err" in box:
        raise box["err"]
    return box["out"]


def _gemini(model, prompt, schema):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema,
                                 "maxOutputTokens": MAX_OUTPUT_TOKENS}}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": key()}, timeout=_timeout(120))
    if r.status_code != 200:
        _fail(r, model)
    text, sent, wrote = _envelope(r.json(), "gemini")
    return _answer(text, schema, sent, wrote)


def _openai(model, prompt, schema):
    headers = {"Authorization": f"Bearer {key()}"} if key() else {}
    if provider() == "openrouter":
        headers["HTTP-Referer"] = "https://qlipit.app"
        headers["X-Title"] = "QlipIt Studio"
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}],
            "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.4, "response_format": {"type": "json_object"}}
    if provider() == "openrouter":
        # A thinking model otherwise spends the whole answer budget on hidden reasoning and returns content
        # null with finish_reason "length": measured with nvidia/nemotron-3.5-lightning. We only want the JSON.
        body["reasoning"] = {"enabled": False}
    r = None
    for _ in range(3):  # the existing capability budget, not a retry of arbitrary bad requests
        r = httpx.post(f"{base_url()}/chat/completions", json=dict(body), headers=headers, timeout=_timeout(180))
        if r.status_code not in (400, 422):
            break
        refusal = r.text.lower()
        if not any(x in refusal for x in ("not supported", "unsupported", "not allowed", "does not support", "unknown parameter", "invalid", "maximum", "max_tokens", "temperature", "response_format", "reasoning")):
            break
        if "temperature" in refusal and "temperature" in body:
            body.pop("temperature")
        elif any(x in refusal for x in ("response_format", "json_object", "format")) and "response_format" in body:
            body.pop("response_format")
        elif "reasoning" in refusal and "reasoning" in body:
            body.pop("reasoning")   # a model that cannot separate thinking from its answer
        elif any(x in refusal for x in ("max_tokens", "max_completion_tokens", "maximum")) and "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        elif "max_completion_tokens" in body:
            body.pop("max_completion_tokens")
        else:
            break
    if r.status_code != 200:
        _fail(r, model)
    text, sent, wrote = _envelope(r.json(), "openai")
    return _answer(text, schema, sent, wrote)


def _anthropic(model, prompt, schema):
    body = {"model": model, "max_tokens": MAX_OUTPUT_TOKENS, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=_timeout(180),
                   headers={"x-api-key": key(), "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model)
    text, sent, wrote = _envelope(r.json(), "anthropic")
    return _answer(text, schema, sent, wrote)
