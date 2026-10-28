"""竞品监控专用网页抓取器。

与调研采集器不同，本模块返回完整归一化正文、响应元数据和明确错误；请求前及
每次重定向后都会校验目标地址，避免订阅 URL 被用于访问本机或内网资源。
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
import socket
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import get_settings
from app.core.content_normalizer import normalize_html


_USER_AGENT = "VerdaAI-Monitor/1.0 (+competitive-intelligence)"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_CONTENT_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class MonitorFetchResult:
    source_url: str
    final_url: str = ""
    ok: bool = False
    not_modified: bool = False
    http_status: int = 0
    content_type: str = ""
    title: str = ""
    text: str = ""
    content_hash: str = ""
    content_length: int = 0
    etag: str = ""
    last_modified: str = ""
    captured_at: str = ""
    redirects: List[str] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MonitorFetchError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 0):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    return not ip.is_global


def validate_public_url(
    url: str,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise MonitorFetchError("invalid_url", "URL must use http or https and include a host")
    if parts.username or parts.password:
        raise MonitorFetchError("invalid_url", "URL credentials are not allowed")
    hostname = parts.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise MonitorFetchError("blocked_address", "local hostnames are not allowed")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise MonitorFetchError("invalid_url", "URL contains an invalid port") from exc

    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
        addresses = [str(literal)]
    except ValueError:
        try:
            addresses = list({
                item[4][0] for item in resolver(hostname, port, type=socket.SOCK_STREAM)
            })
        except OSError as exc:
            raise MonitorFetchError("dns_error", f"DNS resolution failed: {exc}") from exc
    if not addresses:
        raise MonitorFetchError("dns_error", "DNS resolution returned no addresses")
    try:
        if any(_is_forbidden_ip(address) for address in addresses):
            raise MonitorFetchError("blocked_address", "private or special-use addresses are not allowed")
    except ValueError as exc:
        raise MonitorFetchError("dns_error", "DNS resolution returned an invalid address") from exc


def _decode_body(response: httpx.Response, body: bytes) -> str:
    candidates = [response.encoding, "utf-8", "gb18030"]
    seen = set()
    for encoding in candidates:
        if not encoding or encoding.lower() in seen:
            continue
        seen.add(encoding.lower())
        try:
            return body.decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def fetch_monitor_page(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    timeout: Optional[float] = None,
    max_response_bytes: Optional[int] = None,
    max_redirects: Optional[int] = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
    transport: Optional[httpx.BaseTransport] = None,
) -> MonitorFetchResult:
    settings = get_settings()
    timeout = timeout if timeout is not None else settings.monitor_fetch_timeout
    max_response_bytes = (
        max_response_bytes if max_response_bytes is not None else settings.monitor_max_response_bytes
    )
    max_redirects = max_redirects if max_redirects is not None else settings.monitor_max_redirects
    result = MonitorFetchResult(source_url=url, captured_at=_utc_now())
    current_url = url
    seen_urls = set()

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
        ) as client:
            for redirect_index in range(max_redirects + 1):
                validate_public_url(current_url, resolver=resolver)
                if current_url in seen_urls:
                    raise MonitorFetchError("redirect_loop", "redirect loop detected")
                seen_urls.add(current_url)

                conditional_headers = {}
                if redirect_index == 0:
                    if etag:
                        conditional_headers["If-None-Match"] = etag
                    if last_modified:
                        conditional_headers["If-Modified-Since"] = last_modified

                with client.stream("GET", current_url, headers=conditional_headers) as response:
                    result.http_status = response.status_code
                    result.final_url = str(response.url)
                    result.etag = response.headers.get("etag", "")
                    result.last_modified = response.headers.get("last-modified", "")

                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location", "")
                        if not location:
                            raise MonitorFetchError(
                                "invalid_redirect", "redirect response has no Location header",
                                response.status_code,
                            )
                        if redirect_index >= max_redirects:
                            raise MonitorFetchError("too_many_redirects", "redirect limit exceeded")
                        current_url = urljoin(current_url, location)
                        result.redirects.append(current_url)
                        continue

                    if response.status_code == 304:
                        result.ok = True
                        result.not_modified = True
                        return result
                    if response.status_code < 200 or response.status_code >= 300:
                        raise MonitorFetchError(
                            "http_error", f"HTTP {response.status_code}", response.status_code
                        )

                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    result.content_type = content_type
                    if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
                        raise MonitorFetchError(
                            "unsupported_content_type", f"unsupported content type: {content_type}"
                        )
                    declared_length = response.headers.get("content-length", "")
                    if declared_length.isdigit() and int(declared_length) > max_response_bytes:
                        raise MonitorFetchError("response_too_large", "response exceeds size limit")

                    chunks = []
                    received = 0
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_response_bytes:
                            raise MonitorFetchError("response_too_large", "response exceeds size limit")
                        chunks.append(chunk)
                    normalized = normalize_html(_decode_body(response, b"".join(chunks)))
                    if not normalized.text:
                        raise MonitorFetchError("empty_content", "page contains no extractable text")
                    result.ok = True
                    result.title = normalized.title
                    result.text = normalized.text
                    result.content_hash = normalized.content_hash
                    result.content_length = normalized.content_length
                    return result
    except MonitorFetchError as exc:
        result.error_code = exc.code
        result.error_message = str(exc)[:500]
        if exc.http_status:
            result.http_status = exc.http_status
    except httpx.TimeoutException as exc:
        result.error_code = "timeout"
        result.error_message = str(exc)[:500]
    except httpx.HTTPError as exc:
        result.error_code = "network_error"
        result.error_message = str(exc)[:500]
    except Exception as exc:  # noqa: BLE001
        result.error_code = "unexpected_error"
        result.error_message = str(exc)[:500]
    return result
