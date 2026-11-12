"""日志模块：同时输出到控制台和 logs/ 目录下的日志文件。

* 一次性登录写 ``logs/autologin.log``
* 常驻保活写 ``logs/keepalive.log``（它 7×24 跑，必须按天轮转，否则会一直变大）

轮转是自己写的（约 40 行）：Windows 上两个进程同时持有一个日志文件时，
标准库的 ``TimedRotatingFileHandler`` 在重命名那一步会失败刷错误。
现在两个进程各写各的文件，谁也不会抢。
"""
from __future__ import annotations

import logging
import os
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(APP_DIR, "logs")

_LOGGER_NAME = "jitauth"


def _purge_old_logs(keep_days: int) -> None:
    if keep_days <= 0 or not os.path.isdir(LOG_DIR):
        return
    cutoff = time.time() - keep_days * 86400
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _force_utf8_console() -> None:
    """避免计划任务/重定向时中文日志变成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class DailyFileHandler(logging.Handler):
    """按天轮转的文件日志（跨午夜自动切到 ``<name>.YYYY-MM-DD``）。"""

    def __init__(self, path: str, keep_days: int = 14, encoding: str = "utf-8") -> None:
        super().__init__()
        self.path = path
        self.keep_days = keep_days
        self.encoding = encoding
        self._stream = None
        self._day = None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._open(time.localtime())

    def _open(self, tm: time.struct_time) -> None:
        self._day = (tm.tm_year, tm.tm_mon, tm.tm_mday)
        self._stream = open(self.path, "a", encoding=self.encoding, errors="replace")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tm = time.localtime(record.created)
            if (tm.tm_year, tm.tm_mon, tm.tm_mday) != self._day:
                self._rotate(tm)
            if self._stream is None:
                self._open(tm)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 - 日志写失败不能影响主流程
            self.handleError(record)

    def _rotate(self, tm: time.struct_time) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        except OSError:
            pass
        self._stream = None
        stamp = "%04d-%02d-%02d" % self._day
        try:
            os.replace(self.path, f"{self.path}.{stamp}")
        except OSError:
            pass
        self._open(tm)
        self._purge()

    def _purge(self) -> None:
        if self.keep_days <= 0:
            return
        base = os.path.basename(self.path) + "."
        cutoff = time.time() - self.keep_days * 86400
        folder = os.path.dirname(self.path)
        try:
            for name in os.listdir(folder):
                if not name.startswith(base):
                    continue
                full = os.path.join(folder, name)
                try:
                    if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                        os.remove(full)
                except OSError:
                    pass
        except OSError:
            pass

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
        except OSError:
            pass
        self._stream = None
        super().close()


def get_logger(quiet: bool = False, keep_days: int = 14,
               filename: str = "autologin.log") -> logging.Logger:
    """取日志器。

    ``filename`` 不同时会自动换文件（同一个进程里一般只用一个）。
    """
    logger = logging.getLogger(_LOGGER_NAME)
    current = getattr(logger, "_jit_file", None)
    if logger.handlers and current == filename:
        return logger
    if logger.handlers and current != filename:
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass
            logger.removeHandler(handler)

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    _force_utf8_console()
    logger._jit_file = filename  # type: ignore[attr-defined]

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        _purge_old_logs(keep_days)
        handler = DailyFileHandler(os.path.join(LOG_DIR, filename), keep_days=max(1, keep_days))
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except OSError:
        pass  # 日志目录不可写时仍然继续运行

    if sys.stdout is not None and not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger