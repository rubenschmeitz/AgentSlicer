from __future__ import annotations

import base64
import json
import mimetypes
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderTask:
    provider: str
    operation: str
    task_id: str
    status: str
    progress: int
    consumed_credits: float | None
    output_urls: dict[str, str]
    error: str | None = None
    model: str | None = None

    def receipt(self) -> dict[str, Any]:
        value = {
            "provider": self.provider,
            "operation": self.operation,
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "consumed_credits": self.consumed_credits,
            "error": self.error,
        }
        if self.model is not None:
            value["model"] = self.model
        return value


class GenerationProvider:
    id: str

    def submit_reference(
        self,
        prompt: str,
        *,
        model: str | None = None,
        multi_view: bool = True,
    ) -> ProviderTask:
        raise NotImplementedError

    def submit_model(
        self,
        prompt: str,
        *,
        image: Path | None = None,
        model: str | None = None,
    ) -> ProviderTask:
        raise NotImplementedError

    def status(self, operation: str, task_id: str) -> ProviderTask:
        raise NotImplementedError


class MeshyProvider(GenerationProvider):
    id = "meshy"
    base_url = "https://api.meshy.ai"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("MESHY_API_KEY")
        if not self.api_key:
            raise ProviderError("MESHY_API_KEY is not configured")

    def submit_reference(
        self,
        prompt: str,
        *,
        model: str | None = None,
        multi_view: bool = True,
    ) -> ProviderTask:
        operation = "text-to-image"
        payload = {
            "ai_model": model or "nano-banana",
            "prompt": prompt,
            "generate_multi_view": bool(multi_view),
            "remove_background": True,
        }
        response = self._request("POST", "/openapi/v1/text-to-image", payload)
        task_id = _required_string(response, "result")
        return ProviderTask(
            self.id, operation, task_id, "pending", 0, None, {}, model=model or "nano-banana"
        )

    def submit_model(
        self,
        prompt: str,
        *,
        image: Path | None = None,
        model: str | None = None,
    ) -> ProviderTask:
        if image is not None:
            operation = "image-to-3d"
            payload: dict[str, Any] = {
                "image_url": _data_uri(image),
                "ai_model": model or "meshy-7",
                "should_texture": False,
                "should_remesh": True,
                "target_polycount": 100000,
                "target_formats": ["glb", "stl"],
            }
            endpoint = "/openapi/v1/image-to-3d"
        else:
            operation = "text-to-3d"
            payload = {
                "mode": "preview",
                "prompt": prompt,
                "ai_model": model or "meshy-7",
                "should_remesh": True,
                "target_polycount": 100000,
                "target_formats": ["glb", "stl"],
            }
            endpoint = "/openapi/v2/text-to-3d"
        response = self._request("POST", endpoint, payload)
        task_id = _required_string(response, "result")
        return ProviderTask(
            self.id, operation, task_id, "pending", 0, None, {}, model=model or "meshy-7"
        )

    def status(self, operation: str, task_id: str) -> ProviderTask:
        endpoints = {
            "text-to-image": "/openapi/v1/text-to-image",
            "image-to-3d": "/openapi/v1/image-to-3d",
            "text-to-3d": "/openapi/v2/text-to-3d",
        }
        if operation not in endpoints:
            raise ProviderError(f"unsupported Meshy operation: {operation}")
        value = self._request("GET", f"{endpoints[operation]}/{task_id}")
        status = _normal_status(value.get("status"))
        urls: dict[str, str] = {}
        for index, url in enumerate(value.get("image_urls", []) or []):
            if isinstance(url, str):
                urls[f"image-{index + 1}.png"] = url
        model_urls = value.get("model_urls", {})
        if isinstance(model_urls, dict):
            for extension in ("glb", "stl", "obj", "fbx", "3mf"):
                url = model_urls.get(extension)
                if isinstance(url, str):
                    urls[f"model.{extension}"] = url
        error_value = value.get("task_error")
        error = None
        if isinstance(error_value, dict) and error_value.get("message"):
            error = str(error_value["message"])
        return ProviderTask(
            self.id,
            operation,
            task_id,
            status,
            int(value.get("progress", 0) or 0),
            _optional_float(value.get("consumed_credits")),
            urls,
            error,
            model=str(value["ai_model"]) if value.get("ai_model") else None,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _json_request(
            method,
            self.base_url + path,
            self.api_key,
            payload,
        )


class TripoProvider(GenerationProvider):
    id = "tripo"
    base_url = "https://openapi.tripo3d.ai/v3"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("TRIPO_API_KEY")
        if not self.api_key:
            raise ProviderError("TRIPO_API_KEY is not configured")

    def submit_reference(
        self,
        prompt: str,
        *,
        model: str | None = None,
        multi_view: bool = True,
    ) -> ProviderTask:
        operation = "text-to-image"
        response = self._request(
            "POST",
            "/generation/text-to-image",
            {
                "prompt": (
                    prompt
                    if not multi_view
                    else f"{prompt} Arrange the views in one consistent multi-view reference sheet."
                ),
                "model": model or "seedream_v4",
            },
        )
        task_id = _tripo_task_id(response)
        return ProviderTask(
            self.id, operation, task_id, "pending", 0, None, {}, model=model or "seedream_v4"
        )

    def submit_model(
        self,
        prompt: str,
        *,
        image: Path | None = None,
        model: str | None = None,
    ) -> ProviderTask:
        payload: dict[str, Any] = {
            "model": model or "v3.1-20260211",
            "texture": False,
            "pbr": False,
            "geometry_quality": "detailed",
            "face_limit": 100000,
            "export_uv": False,
        }
        if image is None:
            operation = "text-to-model"
            payload["prompt"] = prompt
            endpoint = "/generation/text-to-model"
        else:
            operation = "image-to-model"
            payload["input"] = self._upload(image)
            endpoint = "/generation/image-to-model"
        response = self._request("POST", endpoint, payload)
        task_id = _tripo_task_id(response)
        return ProviderTask(
            self.id,
            operation,
            task_id,
            "pending",
            0,
            None,
            {},
            model=model or "v3.1-20260211",
        )

    def status(self, operation: str, task_id: str) -> ProviderTask:
        value = self._request("GET", f"/tasks/{task_id}")
        data = value.get("data", value)
        if not isinstance(data, dict):
            raise ProviderError("Tripo returned an invalid task object")
        output = data.get("output", {})
        urls: dict[str, str] = {}
        if isinstance(output, dict):
            model_url = output.get("model_url")
            if isinstance(model_url, str):
                suffix = Path(urlparse(model_url).path).suffix.lower() or ".glb"
                urls[f"model{suffix}"] = model_url
            rendered = output.get("rendered_image_url")
            if isinstance(rendered, str):
                urls["preview.png"] = rendered
            for index, image_url in enumerate(output.get("image_urls", []) or []):
                if isinstance(image_url, str):
                    urls[f"image-{index + 1}.png"] = image_url
            image_url = output.get("image_url")
            if isinstance(image_url, str):
                urls["image-1.png"] = image_url
            generated_image = output.get("generated_image")
            if isinstance(generated_image, str):
                urls["image-1.png"] = generated_image
        error = data.get("message") or data.get("error")
        return ProviderTask(
            self.id,
            operation,
            task_id,
            _normal_status(data.get("status")),
            int(data.get("progress", 0) or 0),
            _optional_float(data.get("credits_consumed")),
            urls,
            str(error) if error else None,
            model=str(data["model"]) if data.get("model") else None,
        )

    def _upload(self, path: Path) -> str:
        boundary = f"agent-slicer-{secrets.token_hex(16)}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
        response = _raw_request(
            "POST",
            self.base_url + "/files",
            self.api_key,
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        data = response.get("data", response)
        if not isinstance(data, dict):
            raise ProviderError("Tripo returned an invalid upload response")
        for key in ("file_token", "token"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        raise ProviderError("Tripo upload response did not contain a file token")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _json_request(method, self.base_url + path, self.api_key, payload)


def provider(name: str) -> GenerationProvider:
    if name == "meshy":
        return MeshyProvider()
    if name == "tripo":
        return TripoProvider()
    raise ProviderError(f"unsupported generation provider: {name!r}")


def provider_configuration() -> dict[str, dict[str, Any]]:
    return {
        "meshy": {
            "configured": bool(os.environ.get("MESHY_API_KEY")),
            "credential_environment": "MESHY_API_KEY",
            "capabilities": ["text-to-image", "text-to-3d", "image-to-3d"],
            "base_url": MeshyProvider.base_url,
        },
        "tripo": {
            "configured": bool(os.environ.get("TRIPO_API_KEY")),
            "credential_environment": "TRIPO_API_KEY",
            "capabilities": ["text-to-image", "text-to-model", "image-to-model"],
            "base_url": TripoProvider.base_url,
        },
    }


def download(url: str, destination: Path, *, maximum_bytes: int = 1024**3) -> int:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ProviderError("provider downloads must use HTTPS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "agent-slicer/0.3"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise ProviderError("provider download exceeded the 1 GiB safety limit")
                handle.write(chunk)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return total


def _json_request(
    method: str,
    url: str,
    api_key: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    return _raw_request(method, url, api_key, body, "application/json")


def _raw_request(
    method: str,
    url: str,
    api_key: str,
    body: bytes | None,
    content_type: str,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "agent-slicer/0.3",
    }
    if body is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            detail = raw.decode("utf-8", errors="replace")[:500]
        raise ProviderError(f"provider HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"provider request failed: {exc.reason}") from None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("provider returned a non-object JSON response")
    return value


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _normal_status(value: Any) -> str:
    text = str(value or "pending").lower().replace("_", "-")
    mapping = {
        "pending": "pending",
        "queued": "pending",
        "in-progress": "running",
        "running": "running",
        "success": "succeeded",
        "succeeded": "succeeded",
        "failed": "failed",
        "banned": "failed",
        "canceled": "canceled",
        "cancelled": "canceled",
    }
    return mapping.get(text, text)


def _required_string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ProviderError(f"provider response omitted {key!r}")
    return result


def _tripo_task_id(value: dict[str, Any]) -> str:
    data = value.get("data", value)
    if not isinstance(data, dict):
        raise ProviderError("Tripo returned an invalid task response")
    return _required_string(data, "task_id")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
