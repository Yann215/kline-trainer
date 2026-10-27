"""Local, single-flight TSP data synchronization jobs."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class DataSync:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.Lock()
        self.job = {"status": "idle", "message": "尚未开始同步", "result": None}
        self.settings = self.root / "sync-settings.json"

    def source(self) -> str:
        try:
            return str(json.loads(self.settings.read_text(encoding="utf-8")).get("source", ""))
        except (OSError, ValueError, TypeError):
            return ""

    def view(self) -> dict:
        with self.lock:
            return {**self.job, "source": self.source()}

    def start(self, source: str) -> dict:
        source = source.strip()
        if not source:
            raise ValueError("请填写 TSP 行情数据目录")
        path = Path(source).expanduser()
        if not path.is_absolute():
            raise ValueError("请填写完整的本地目录路径")
        path = path.resolve()
        if not path.is_dir():
            raise ValueError("数据目录不存在，请检查路径")
        if path.is_relative_to(self.root) or self.root.is_relative_to(path):
            raise ValueError("源目录与盘感训练的数据目录不能互相包含")
        with self.lock:
            if self.job["status"] == "running":
                raise RuntimeError("已有同步任务正在运行，请等待完成")
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / "sync-settings.tmp"
            tmp.write_text(json.dumps({"source": str(path)}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.settings)
            self.job = {"status": "running", "message": "正在读取、校验并整理行情，请耐心等待", "result": None}

        def worker():
            try:
                from app.cli import import_tsp_market
                result = import_tsp_market(path, self.root)
                with self.lock:
                    self.job = {"status": "complete", "message": "同步完成，重启训练服务后启用新行情", "result": result}
            except (ValueError, OSError) as exc:
                with self.lock:
                    self.job = {"status": "failed", "message": str(exc), "result": None}
            except Exception:
                logger.exception("market synchronization failed")
                with self.lock:
                    self.job = {"status": "failed", "message": "同步失败，请检查源行情及服务日志", "result": None}

        threading.Thread(target=worker, name="data-sync", daemon=True).start()
        return self.view()
