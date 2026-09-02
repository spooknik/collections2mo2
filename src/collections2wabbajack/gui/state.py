"""The wizard's accumulated state, passed by reference to every page."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import api


@dataclass
class WizardState:
    # -- sign-in --------------------------------------------------------------
    api_key: str = ""
    signin: api.SignInResult | None = None

    # -- collection -------------------------------------------------------------
    collection_url: str = ""
    collection_summary: api.CollectionSummary | None = None
    selected_revision: int | None = None
    survey_summary: api.SurveySummary | None = None

    # -- location / game --------------------------------------------------------
    instance_dir: Path | None = None
    game_path: Path | None = None
    stock_game: bool = True

    # -- tools --------------------------------------------------------------
    tool_ids: list[str] = field(default_factory=list)

    # -- display --------------------------------------------------------------
    resolution: str = "keep"
    vsync: str = "keep"
    window: str = "keep"

    jobs: int = 4

    # -- run result -------------------------------------------------------------
    run_succeeded: bool | None = None
