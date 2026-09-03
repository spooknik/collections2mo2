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

    # -- location / game --------------------------------------------------------
    # An instance folder the run must go into rather than freshly choose: set by the
    # Manage page's "Set up a collection here" for an instance whose collection was
    # removed, so `create` reuses the folder's existing `downloads/` (see
    # `LocationPage.on_enter`). None for an ordinary new-instance run.
    preset_instance_dir: Path | None = None
    instance_dir: Path | None = None
    game_path: Path | None = None
    stock_game: bool = True
    # The Location page's advisory game-version check: ("match"|"mismatch"|"unknown",
    # message), or None when the collection records no target version. The Review page
    # restates it; nothing in the run acts on it.
    game_version_check: tuple[str, str] | None = None
    installed_game_version: str | None = None

    # -- tools --------------------------------------------------------------
    tool_ids: list[str] = field(default_factory=list)

    # -- display --------------------------------------------------------------
    resolution: str = "keep"
    vsync: str = "keep"
    window: str = "keep"

    jobs: int = 4

    # -- run result -------------------------------------------------------------
    run_succeeded: bool | None = None

    def reset_for_new_run(self, *, keep_preset: bool = False) -> None:
        """ "Back to start": clear every choice made for a create run (or picked up by
        Manage), keeping the signed-in account (`api_key` / `signin`) intact.

        `keep_preset` keeps `preset_instance_dir`; the only caller that asks for that
        is the "set up a collection in this existing folder" jump, which resets the
        wizard first and then wants the folder to survive into the new run. (It sets
        the field after the reset anyway, so this is belt and braces.)
        """
        if not keep_preset:
            self.preset_instance_dir = None
        self.collection_url = ""
        self.collection_summary = None
        self.selected_revision = None
        self.instance_dir = None
        self.game_path = None
        self.stock_game = True
        self.game_version_check = None
        self.installed_game_version = None
        self.tool_ids = []
        self.resolution = "keep"
        self.vsync = "keep"
        self.window = "keep"
        self.jobs = 4
        self.run_succeeded = None
