"""FastAPI REST API and dashboard serving for ChronosMatch."""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import load_settings
from .matching_engine import CancelStatus, MatchingEngine
from .models import (
    DuplicateOrderError,
    OrderValidationError,
    validate_order_id,
    validate_price,
    validate_quantity,
    validate_side,
    validate_symbol,
)
from .persistence import SQLiteJournal

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_FILE = PROJECT_ROOT / "frontend" / "index.html"


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(..., description="Trading symbol, e.g. AAPL")
    side: str = Field(..., description="BUY or SELL")
    price: str | int = Field(..., description='Positive decimal, e.g. "123.4500"')
    quantity: int = Field(..., gt=0)
    order_id: str | None = None

    @field_validator("symbol")
    @classmethod
    def symbol_valid(cls, value: str) -> str:
        return validate_symbol(value)

    @field_validator("side")
    @classmethod
    def side_valid(cls, value: str) -> str:
        return validate_side(value).value

    @field_validator("price")
    @classmethod
    def price_valid(cls, value: str | int) -> str:
        return str(validate_price(value))

    @field_validator("quantity")
    @classmethod
    def quantity_valid(cls, value: int) -> int:
        return validate_quantity(value)

    @field_validator("order_id")
    @classmethod
    def order_id_valid(cls, value: str | None) -> str | None:
        return validate_order_id(value) if value is not None else None


def create_app(
    engine: MatchingEngine | None = None,
    db_path: str | Path | None = None,
    persist: bool | None = None,
) -> FastAPI:
    """Create an app; tests may inject an engine and disable persistence."""
    settings = load_settings()
    use_persistence = (engine is None) if persist is None else persist
    resolved_db = Path(db_path) if db_path is not None else settings.db_path
    mutation_lock = threading.RLock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if use_persistence:
            journal = SQLiteJournal(resolved_db)
            recovered_engine = MatchingEngine()
            replayed = journal.replay(recovered_engine)
            app.state.engine = recovered_engine
            app.state.journal = journal
            app.state.replayed_commands = replayed
            logger.info("Recovered %d commands from %s", replayed, resolved_db)
        else:
            app.state.engine = engine or MatchingEngine()
            app.state.journal = None
            app.state.replayed_commands = 0
        yield
        if app.state.journal is not None:
            app.state.journal.close()

    app = FastAPI(
        title="ChronosMatch API",
        version="1.1.0",
        description=(
            "Limit-order matching simulator with price-time priority, "
            "resting-order-price execution, SQLite recovery, and a dashboard."
        ),
        lifespan=lifespan,
    )
    app.state.engine = engine or MatchingEngine()
    app.state.journal = None
    app.state.replayed_commands = 0

    def get_engine() -> MatchingEngine:
        return app.state.engine

    def get_journal() -> SQLiteJournal | None:
        return app.state.journal

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        if not DASHBOARD_FILE.is_file():
            raise HTTPException(status_code=500, detail="Dashboard file is missing")
        return FileResponse(DASHBOARD_FILE, media_type="text/html")

    @app.get("/api", tags=["system"])
    def api_info() -> dict[str, str]:
        return {"name": "ChronosMatch API", "docs": "/docs", "dashboard": "/"}

    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "persistence": "sqlite" if get_journal() else "disabled",
            "replayed_commands": app.state.replayed_commands,
        }

    @app.post("/orders", status_code=status.HTTP_201_CREATED, tags=["orders"])
    def create_order(payload: OrderCreate) -> dict[str, Any]:
        with mutation_lock:
            try:
                result = get_engine().submit(
                    symbol=payload.symbol,
                    side=payload.side,
                    price=payload.price,
                    quantity=payload.quantity,
                    order_id=payload.order_id,
                )
            except DuplicateOrderError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except OrderValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            # Store the generated ID as well, so startup replay is deterministic.
            if get_journal() is not None:
                try:
                    get_journal().append("submit", {
                        "symbol": result.order.symbol,
                        "side": result.order.side.value,
                        "price": str(result.order.price),
                        "quantity": result.order.quantity,
                        "order_id": result.order.order_id,
                    })
                except Exception as exc:
                    logger.exception("Could not persist submitted order")
                    raise HTTPException(
                        status_code=503,
                        detail="Order entered the in-memory engine but could not be persisted; check server logs",
                    ) from exc
            return {
                "order": result.order.to_dict(),
                "trades": [trade.to_dict() for trade in result.trades],
                "message": "Order accepted",
            }

    @app.get("/orders", tags=["orders"])
    def list_orders(
        symbol: str | None = None,
        side: str | None = Query(default=None, description="BUY or SELL"),
        active_only: bool = True,
    ) -> dict[str, Any]:
        try:
            orders = get_engine().get_orders(
                symbol=validate_symbol(symbol) if symbol is not None else None,
                side=validate_side(side) if side is not None else None,
                active_only=active_only,
            )
        except (OrderValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"count": len(orders), "orders": [o.to_dict() for o in orders]}

    @app.get("/orders/{order_id}", tags=["orders"])
    def get_order(order_id: str) -> dict[str, Any]:
        order = get_engine().get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return order.to_dict()

    @app.delete("/orders/{order_id}", tags=["orders"])
    def cancel_order(order_id: str) -> dict[str, Any]:
        with mutation_lock:
            result = get_engine().cancel(order_id)
            if result.status is CancelStatus.NOT_FOUND:
                raise HTTPException(status_code=404, detail=result.message)
            if result.status in (CancelStatus.ALREADY_FILLED, CancelStatus.ALREADY_CANCELLED):
                raise HTTPException(status_code=409, detail=result.message)
            if get_journal() is not None:
                try:
                    get_journal().append("cancel", {"order_id": order_id})
                except Exception as exc:
                    logger.exception("Could not persist cancellation")
                    raise HTTPException(
                        status_code=503,
                        detail="Order was cancelled in memory but cancellation could not be persisted; check server logs",
                    ) from exc
            return {
                "order_id": result.order_id,
                "status": result.status.value,
                "message": result.message,
                "order": result.order.to_dict() if result.order else None,
            }

    @app.get("/order-book/{symbol}", tags=["market data"])
    def order_book(
        symbol: str,
        depth: int | None = Query(default=10, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            return get_engine().order_book_snapshot(validate_symbol(symbol), depth)
        except OrderValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/trades", tags=["market data"])
    def list_trades(
        symbol: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            normalized = validate_symbol(symbol) if symbol is not None else None
            trades = get_engine().recent_trades(limit=limit, symbol=normalized)
        except OrderValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"count": len(trades), "trades": [t.to_dict() for t in trades]}

    @app.get("/symbols", tags=["market data"])
    def symbols() -> dict[str, list[str]]:
        return {"symbols": get_engine().symbols()}

    @app.get("/stats", tags=["system"])
    def stats() -> dict[str, Any]:
        return get_engine().stats()

    return app


app = create_app()
