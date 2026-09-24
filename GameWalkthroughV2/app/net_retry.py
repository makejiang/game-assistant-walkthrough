"""网络请求自动重试：失败最多重试 3 次，每次间隔 20 秒。

覆盖两类故障：
  - 攻略站点（gamersky）的页面/图片下载（game_walkthrough_downloader.py）
  - 游戏助手服务端的通信（walkthrough_service_importer.py 的全部 HTTP 调用）

用法：包住现成的 requests.Session，调用方代码不变：
    session = RetryingSession(requests.Session(), notify=print)
    session.get(url, timeout=20)

重试策略：
  - requests.RequestException（超时/连接失败等“请求本身失败”）→ 重试；
  - 响应状态码在 retry_statuses（默认 500/502/503/504，服务端暂时不可用）→ 重试；
  - 其余状态码原样返回（参数/权限类错误重试也不会成功）。

每次重试前通过 notify（人类可读提示，会进下载进度条/控制台）与 logging（文件
日志）上报。超过次数后：异常场景原样抛出最后一次异常；5xx 场景返回最后一次
响应，交由调用方的 raise_for_status / _ensure_http_success 产出错误信息。
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Callable

import requests

DEFAULT_ATTEMPTS = 3      # 失败后自动重试的次数
DEFAULT_INTERVAL = 20.0   # 两次尝试之间的间隔（秒）
DEFAULT_RETRY_STATUSES = frozenset({500, 502, 503, 504})

_DESCRIPTION_MAX = 120

_logger = logging.getLogger("net_retry")


def _shorten(text: str, limit: int = _DESCRIPTION_MAX) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class RetryingSession:
    """requests.Session 的重试包装：对外暴露与 Session 相同的常用方法。"""

    def __init__(
        self,
        session: requests.Session,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        interval: float = DEFAULT_INTERVAL,
        notify: Callable[[str], None] | None = None,
        retry_statuses: Any = DEFAULT_RETRY_STATUSES,
    ) -> None:
        self._session = session
        self._attempts = max(0, int(attempts))
        self._interval = max(0.0, float(interval))
        self._notify = notify
        self._retry_statuses = set(retry_statuses)

    # 透传常用属性（调用方可能往 session.headers 里塞 UA 等）
    @property
    def headers(self):
        return self._session.headers

    @property
    def cookies(self):
        return self._session.cookies

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------ 请求
    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        label = f"{method.upper()} {url}"
        return self._send(label, lambda: self._session.request(method, url, **kwargs))

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"GET {url}", lambda: self._session.get(url, **kwargs))

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"POST {url}", lambda: self._session.post(url, **kwargs))

    def put(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"PUT {url}", lambda: self._session.put(url, **kwargs))

    def delete(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"DELETE {url}", lambda: self._session.delete(url, **kwargs))

    def head(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"HEAD {url}", lambda: self._session.head(url, **kwargs))

    def options(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"OPTIONS {url}", lambda: self._session.options(url, **kwargs))

    def patch(self, url: str, **kwargs) -> requests.Response:
        return self._send(f"PATCH {url}", lambda: self._session.patch(url, **kwargs))

    def __enter__(self) -> "RetryingSession":
        self._session.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        self._session.__exit__(*exc_info)

    # ------------------------------------------------------------------ 内部
    def _send(self, description: str, send: Callable[[], requests.Response]) -> requests.Response:
        last_exc: requests.RequestException | None = None
        for attempt in range(self._attempts + 1):
            try:
                response = send()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self._attempts:
                    break  # 次数用尽：抛出最后一次异常
                reason = f"{type(exc).__name__}: {_shorten(exc, 200)}"
            else:
                if response.status_code not in self._retry_statuses:
                    return response
                # 服务端暂时不可用：重试；次数用尽则把响应交回调用方处理
                if attempt >= self._attempts:
                    return response
                reason = f"HTTP {response.status_code}"
            self._report(reason, attempt, description)
            if self._interval > 0:
                time.sleep(self._interval)
        assert last_exc is not None
        raise last_exc

    def _report(self, reason: str, attempt: int, description: str) -> None:
        message = (
            f"网络请求失败({reason})，"
            f"{self._interval:.0f} 秒后自动重试(第 {attempt + 1}/{self._attempts} 次): "
            f"{_shorten(description)}"
        )
        _logger.warning(message)
        if self._notify is not None:
            with contextlib.suppress(Exception):
                self._notify(message)
