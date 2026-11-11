# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ollama's native API, shared by the desktop app and the planner.

Cloud uses https://ollama.com directly; no local Ollama server is required.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .llm import LLMPlanner
from .planner import PlannerError

CLOUD_URL = "https://ollama.com"


def endpoint(provider: str, local_url: str = "http://127.0.0.1:11434") -> str:
    if provider == "ollama-cloud":
        return CLOUD_URL
    if provider != "ollama-local":
        raise ValueError("Choose Ollama Cloud, local Ollama, or offline commands")
    parsed = urllib.parse.urlsplit(local_url)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("Local Ollama must use a loopback URL, such as http://127.0.0.1:11434")
    return local_url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a cloud credential to a redirect destination.
        return None


def api_request(base_url: str, path: str, api_key: str = "", body: dict | None = None,
                timeout: float = 45.0) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + path, data=data, headers=headers)
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise PlannerError("Ollama returned a response larger than 4 MB")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        explanations = {401: "API key was rejected. Check your Ollama key.",
                        403: "Access was denied. Check your Ollama account and model access.",
                        404: "Model or endpoint not found. Refresh the model list.",
                        429: "Ollama rate limit reached. Wait a moment and try again."}
        raise PlannerError(explanations.get(exc.code, f"Ollama request failed (HTTP {exc.code})")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlannerError("Could not reach Ollama. Check the connection and endpoint.") from None
    except (ValueError, UnicodeError):
        raise PlannerError("Ollama returned an invalid JSON response") from None


def list_models(base_url: str, api_key: str = "") -> list[str]:
    data = api_request(base_url, "/api/tags", api_key, timeout=15)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise PlannerError("Ollama returned an invalid model list")
    return sorted({item["name"] for item in data["models"]
                   if isinstance(item, dict) and isinstance(item.get("name"), str)})


class OllamaPlanner(LLMPlanner):
    def _complete(self, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "options": {"temperature": self.temperature}}
        # Cloud models do not all implement JSON mode; the system prompt and
        # inherited reply validation/repair work without provider-specific formats.
        if self.json_mode:
            body["format"] = "json"
        data = api_request(self.base_url, "/api/chat", self.api_key, body, self.timeout)
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError):
            raise PlannerError("Ollama returned an unexpected chat response") from None
        if not isinstance(content, str) or not content.strip():
            raise PlannerError("Ollama returned an empty reply")
        return content
