from contextlib import asynccontextmanager
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.custom.training import router
from app.dataset import LocalRepository, LocalStrategies, data_root, publish
from app.demo import demo_bars
from app.services.training import TrainingMarket, TrainingService, TrainingStore
from app.services.training_selection import catalog
from app.services.ai_provider import ai_configured

PROJECT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT / ".env")


def create_app(root=None, *, seed_demo=True, frontend=None):
    root = Path(root) if root is not None else data_root()

    @asynccontextmanager
    async def lifespan(app):
        if not (root / "manifest.json").exists():
            if not seed_demo:
                raise RuntimeError("尚未导入数据，请使用 kline-trainer demo 或 import-csv")
            publish(root, demo_bars(), label="合成演示行情", price_mode="raw")
        repo = LocalRepository(root)
        engine = LocalStrategies(repo)
        app.state.training_service = TrainingService(TrainingStore(root), TrainingMarket(repo, engine))
        yield

    app = FastAPI(title="Kline Trainer", version="0.1.0", lifespan=lifespan)
    app.include_router(router)

    @app.get("/api/health")
    def health():
        repo = app.state.training_service.market.repo
        return dict(status="ok", dataset=repo.manifest["label"],
                    price_mode=repo.manifest["price_mode"], symbols=repo.manifest["symbols"],
                    start=repo.manifest["start"], end=repo.manifest["end"],
                    ai_configured=ai_configured())

    @app.get("/api/custom/training/available-selection-types")
    def available_types():
        available = app.state.training_service.market.repo.manifest.get("limit_counters_available", False)
        limited = {"limit_up", "limit_down", "consecutive_up", "consecutive_down"}
        return [row for row in catalog() if available or row["id"] not in limited]

    dist = Path(frontend) if frontend else Path(os.environ.get("TRAINER_WEB_DIR", PROJECT / "frontend" / "dist"))

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "API不存在")
        target = (dist / path).resolve()
        if target.is_relative_to(dist.resolve()) and target.is_file():
            return FileResponse(target)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        raise HTTPException(503, "前端尚未构建：请在 frontend 中运行 npm ci && npm run build")

    return app


app = create_app()
