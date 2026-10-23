"""Bounded local preparation and server-only next-topic preloading."""

# ruff: noqa: RUF001 -- Chinese product copy.
from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid

from app.services.training import TrainingError

logger = logging.getLogger(__name__)
PRELOAD_TTL = 30 * 60
MAX_PRELOAD_BYTES = 8 * 1024 * 1024
MAX_REQUEST_ALIASES = 256


class PreparationCancelledError(Exception):
    pass


def _settings(config):
    return {
        k: v
        for k, v in config.items()
        if k not in {"request_id", "previous_session_id", "preload_id", "indicators"}
    }


class TrainingJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.changed = threading.Condition(self.lock)
        self.aliases = {}

    def _existing_request(self, config, preload):
        alias = self.aliases.get(config["request_id"])
        if alias and alias[0] in self.jobs:
            if not preload or alias[1] != config:
                raise TrainingError("相同请求标识不能用于不同训练设置", 409)
            return self.jobs[alias[0]]
        for previous in self.jobs.values():
            if previous["config"]["request_id"] == config["request_id"]:
                if previous["config"] != config or previous.get("preload", False) != preload:
                    raise TrainingError("相同请求标识不能用于不同训练设置", 409)
                return previous
        return None

    def _remember_alias(self, config, job):
        self.aliases[config["request_id"]] = (job["id"], config)
        while len(self.aliases) > MAX_REQUEST_ALIASES:
            self.aliases.pop(next(iter(self.aliases)))

    def _purge_preloads(self):
        now = time.monotonic()
        for job in self.jobs.values():
            if (
                job.get("preload")
                and job.get("prepared")
                and now - job.get("finished", now) > PRELOAD_TTL
            ):
                job.pop("prepared", None)

    @staticmethod
    def _view(job):
        preload = job.get("preload", False)
        ready = preload and bool(job.get("prepared")) and not job.get("claimed_by")
        return dict(
            id=job["id"],
            status=job["status"],
            phase=job["phase"],
            preload=preload,
            preload_ready=ready,
            elapsed=round(job.get("finished", time.monotonic()) - job["created"], 1),
            completed=job["completed"],
            total=job["total"],
            result=None if preload else copy.deepcopy(job.get("result")),
            error=job.get("error"),
        )

    def get(self, job_id):
        with self.lock:
            self._purge_preloads()
            if job_id not in self.jobs:
                raise TrainingError("抽题任务已失效，请重新开始；已有训练记录仍保留", 404)
            return self._view(self.jobs[job_id])

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise TrainingError("抽题任务不存在", 404)
            job = self.jobs[job_id]
            if job["status"] in {"queued", "running", "cancelling"}:
                job["cancel"].set()
                job.update(status="cancelling", phase="已请求取消，等待当前阶段安全退出")
            elif job.get("preload"):
                job.pop("prepared", None)
            self.changed.notify_all()
            return self._view(job)

    def start(self, service, config, preload=False):
        config = copy.deepcopy(config)
        with self.lock:
            self._purge_preloads()
            previous = self._existing_request(config, preload)
            if previous is not None:
                return self._view(previous)
        # Version checks may read local metadata; keep them outside the job lock.
        revision = service.preload_revision(config) if preload and hasattr(service, "preload_revision") else None
        with self.lock:
            self._purge_preloads()
            previous = self._existing_request(config, preload)
            if previous is not None:
                return self._view(previous)
            if preload:
                reusable = next((j for j in self.jobs.values()
                    if j.get("preload") and not j.get("claimed_by")
                    and j.get("revision") == revision and _settings(j["config"]) == _settings(config)
                    and (j["status"] in {"queued", "running"} or (j["status"] == "complete" and j.get("prepared")))), None)
                if reusable is not None:
                    self._remember_alias(config, reusable)
                    return self._view(reusable)
                # The replacement waits without I/O until obsolete work exits.
                for old in self.jobs.values():
                    if old.get("preload") and not old.get("claimed_by"):
                        old.pop("prepared", None)
                        if old["status"] in {"queued", "running", "cancelling"}:
                            old["cancel"].set()
                            if old.get("waiting"):
                                old.update(status="cancelled", phase="旧设置预加载已取消", finished=time.monotonic())
                                old["ready"].set()
                            else:
                                old.update(status="cancelling", phase="切换设置，等待旧预加载退出")
                self.changed.notify_all()
            working = [j for j in self.jobs.values()
                if j["status"] in {"queued", "running", "cancelling"} and not j.get("waiting")]
            if not preload and len(working) >= 2:
                raise TrainingError("已有两个抽题任务或后台正在准备，请等待完成或取消后再试", 409)
            while len(self.jobs) >= 64:
                old = next(
                    (
                        key
                        for key, j in self.jobs.items()
                        if j["status"] in {"complete", "failed", "cancelled"}
                    ),
                    None,
                )
                if old is None:
                    break
                del self.jobs[old]
            job = dict(
                id=uuid.uuid4().hex,
                status="queued",
                phase="等待前一个准备任务结束" if preload and working else "后台准备下一题" if preload else "准备抽取历史题目",
                created=time.monotonic(),
                completed=0,
                total=0,
                cancel=threading.Event(),
                ready=threading.Event(),
                config=config,
                preload=preload,
                revision=revision,
                waiting=preload,
            )
            self.jobs[job["id"]] = job
            staged = self.jobs.get(config.get("preload_id")) if not preload else None
            if staged is not None and (
                not staged.get("preload")
                or staged.get("claimed_by")
                or _settings(staged["config"]) != _settings(config)
            ):
                staged = None
            if staged is not None:
                staged["claimed_by"] = job["id"]
                job["waiting_for"] = staged["id"]

        def progress(phase, completed=0, total=0):
            if job["cancel"].is_set():
                raise PreparationCancelledError()
            with self.lock:
                job.update(status="running", phase=phase, completed=completed, total=total)

        def worker():
            try:
                if preload:
                    with self.changed:
                        while True:
                            if job["cancel"].is_set():
                                raise PreparationCancelledError()
                            blockers = [j for j in self.jobs.values()
                                if j is not job and j["status"] in {"queued", "running", "cancelling"}
                                and not j.get("waiting") and j.get("waiting_for") != job["id"]]
                            if not blockers:
                                job["waiting"] = False
                                break
                            self.changed.wait(.25)
                    result = service.create(
                        {**config, "previous_session_id": None}, progress=progress, defer_save=True
                    )
                    size = len(
                        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
                    )
                    if size > MAX_PRELOAD_BYTES:
                        raise TrainingError("本题数据较大，点击时再准备；当前训练不受影响")
                    progress("下一题已预加载")
                    with self.lock:
                        job.update(status="complete", phase="下一题已预加载", prepared=result,
                                   revision=service.preload_revision(config, session=result) if hasattr(service, "preload_revision") else None)
                else:
                    result = None
                    if staged is not None:
                        progress("等待后台预加载完成")
                        while not staged["ready"].wait(0.25):
                            progress("等待后台预加载完成")
                        progress("校验预加载题目")
                        with self.lock:
                            job.pop("waiting_for", None)
                            prepared = staged.pop("prepared", None)
                        if prepared is not None:
                            result = service.activate_preloaded(prepared, config, progress=progress)
                    if result is None:
                        result = service.create(config, progress=progress)
                    with self.lock:
                        job.update(status="complete", phase="匿名训练题目已就绪", result=result)
            except PreparationCancelledError:
                with self.lock:
                    job.update(status="cancelled", phase="抽题已取消；原训练记录保留")
            except TrainingError as exc:
                with self.lock:
                    job.update(status="failed", phase="抽题未完成", error=str(exc))
            except Exception:
                logger.exception("training preparation failed")
                with self.lock:
                    job.update(
                        status="failed",
                        phase="抽题未完成",
                        error="训练数据暂不可用，请检查本地行情或稍后重试",
                    )
            finally:
                with self.lock:
                    job["finished"] = time.monotonic()
                    if (
                        job["status"] == "cancelled"
                        and staged is not None
                        and staged.get("claimed_by") == job["id"]
                    ):
                        staged.pop("claimed_by", None)
                    self.changed.notify_all()
                job["ready"].set()

        threading.Thread(
            target=worker,
            name="training-preload" if preload else "training-preparation",
            daemon=True,
        ).start()
        # Ready hits normally finish in this short wait, avoiding a 1s client poll.
        if staged is not None and staged["ready"].is_set():
            job["ready"].wait(0.2)
        return self.get(job["id"])
