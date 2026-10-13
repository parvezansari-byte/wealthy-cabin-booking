"""Data loading helpers shared by the views."""
from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from core.booking_service import Interval, parse_intervals
from core.errors import AppError
from ui.context import AppContext


def load_cabins(ctx: AppContext) -> list[dict]:
    return ctx.repo.list_cabins()


def load_intervals(ctx: AppContext, start: datetime, end: datetime) -> list[Interval]:
    return parse_intervals(ctx.repo.availability(start, end))


def guarded(fn, *args, **kwargs):
    """Run a data call; on failure show a friendly error and stop this page render."""
    try:
        return fn(*args, **kwargs)
    except AppError as err:
        st.error(err.message)
        st.stop()


def next_days(start: datetime, days: int) -> datetime:
    return start + timedelta(days=days)
