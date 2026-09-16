"""The count wizard: one validation from footage to finalised report.

The wizard was one 2,100-line module. It is now a package, split by what each part does,
with no change to what any of it does:

    state.py   the Wizard itself: its state file, the steps, the sensor's numbers, the
               counting and checking, the pictures and the report's data
    items.py   what a check asks a person about, and the settings that decide it
    jobs.py    running gate and detect as child processes, and following them

Everything other modules used is still imported from `crossing_count.wizard`.
"""

from __future__ import annotations

from .items import (
    AUDIT_MIN,
    AUDIT_SHARE,
    CHOICES,
    CLIP_AFTER_S,
    CLIP_BEFORE_S,
    DIRECTIONS,
    FOUND,
    GROUND_TRUTH_SPEC,
    LABELS,
    MAX_GROUP,
    MIN_WATCHED_PCT,
    MODELS,
    POSSIBLE_REASONS,
    PROMPT_PRIORITY,
    RULE_CHOICES,
    TWIN_WINDOW_S,
    VIDEO_EXTS,
    accuracy_range,
    default_folders,
    distinct_people,
    list_videos,
    mark_twins,
    prompt_priority,
    review_items,
    row_numbers,
    watch_stretches,
)
from .jobs import ROOT, Progress, pipeline_commands, tool
from .state import Wizard, WizardError

__all__ = [
    "AUDIT_MIN",
    "AUDIT_SHARE",
    "CHOICES",
    "CLIP_AFTER_S",
    "CLIP_BEFORE_S",
    "DIRECTIONS",
    "FOUND",
    "GROUND_TRUTH_SPEC",
    "LABELS",
    "MAX_GROUP",
    "MIN_WATCHED_PCT",
    "MODELS",
    "POSSIBLE_REASONS",
    "PROMPT_PRIORITY",
    "ROOT",
    "RULE_CHOICES",
    "TWIN_WINDOW_S",
    "VIDEO_EXTS",
    "Progress",
    "Wizard",
    "WizardError",
    "accuracy_range",
    "default_folders",
    "distinct_people",
    "list_videos",
    "mark_twins",
    "pipeline_commands",
    "prompt_priority",
    "review_items",
    "row_numbers",
    "tool",
    "watch_stretches",
]
