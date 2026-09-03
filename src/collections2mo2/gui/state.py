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

    def reset_for_new_run(self) -> None:
        """ "Back to start": clear every choice made for a create run (or picked up by
        Manage), keeping the signed-in account (`api_key` / `signin`) intact."""
        self.collection_url = ""
        self.collection_summary = None
        self.selected_revision = None
        self.survey_summary = None
        self.instance_dir = None
        self.game_path = None
        self.stock_game = True
        self.tool_ids = []
        self.resolution = "keep"
        self.vsync = "keep"
        self.window = "keep"
        self.jobs = 4
        self.run_succeeded = None
