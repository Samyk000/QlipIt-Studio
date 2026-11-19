"""Domain state definitions and validated state transitions.

Follows python-patterns ('Explicit is better than implicit') and error-handling."""
from __future__ import annotations

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from typing import Any


class InvalidStateTransitionError(ValueError):
    """Raised when an illegal lifecycle state jump is attempted."""

    def __init__(self, entity: str, current: str, target: str, allowed: set[str]):
        super().__init__(
            f"Cannot transition {entity} from '{current}' to '{target}'. "
            f"Allowed next states: {sorted(list(allowed)) or 'None (terminal state)'}"
        )
        self.entity = entity
        self.current = current
        self.target = target
        self.allowed = allowed


# ---------------------------------------------------------------- Job & Pipeline States
class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class JobStage(StrEnum):
    WAITING = "Waiting"
    PREPARING_MODEL = "Getting speech model"
    GETTING_AUDIO = "Getting the audio"
    TRANSCRIBING = "Transcribing"
    FINDING_CLIPS = "Finding clips"
    MAKING_CLIPS = "Making clips"
    PICKING_UP = "Picking up where it stopped"
    STOPPED = "Stopped by you"
    DONE = "Done"


JOB_TRANSITIONS: dict[str, set[str]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.DONE, JobStatus.ERROR},  # DONE when cancelled while queued
    JobStatus.RUNNING: {JobStatus.DONE, JobStatus.ERROR},
    JobStatus.DONE: {JobStatus.QUEUED},  # Allows re-queuing / restarting
    JobStatus.ERROR: {JobStatus.QUEUED},  # Allows retry
}


# ---------------------------------------------------------------- Clip States
class ClipStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    DONE = "done"
    ERROR = "error"


CLIP_TRANSITIONS: dict[str, set[str]] = {
    ClipStatus.QUEUED: {ClipStatus.RENDERING, ClipStatus.ERROR},
    ClipStatus.RENDERING: {ClipStatus.DONE, ClipStatus.ERROR},
    ClipStatus.DONE: {ClipStatus.QUEUED},  # User trims or edits clip
    ClipStatus.ERROR: {ClipStatus.QUEUED},  # User presses Save and re-make
}


# ---------------------------------------------------------------- Voiceover Modes
class VoiceoverMode(StrEnum):
    ORIGINAL = "orig"
    REPLACE = "mute"
    DUCK = "duck"


# ---------------------------------------------------------------- Speech Model States
class ModelStatus(StrEnum):
    NOT_DOWNLOADED = "not_downloaded"
    COPYING_BUNDLED = "copying_bundled"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    READY = "ready"
    ERROR = "error"


MODEL_TRANSITIONS: dict[str, set[str]] = {
    ModelStatus.NOT_DOWNLOADED: {ModelStatus.COPYING_BUNDLED, ModelStatus.DOWNLOADING, ModelStatus.READY},
    ModelStatus.COPYING_BUNDLED: {ModelStatus.READY, ModelStatus.DOWNLOADING, ModelStatus.ERROR},
    ModelStatus.DOWNLOADING: {ModelStatus.EXTRACTING, ModelStatus.READY, ModelStatus.ERROR},
    ModelStatus.EXTRACTING: {ModelStatus.READY, ModelStatus.ERROR},
    ModelStatus.READY: {ModelStatus.NOT_DOWNLOADED},  # Model deleted from settings
    ModelStatus.ERROR: {ModelStatus.DOWNLOADING, ModelStatus.NOT_DOWNLOADED},
}


# ---------------------------------------------------------------- Upload States
class UploadStatus(StrEnum):
    INITIATED = "initiated"
    RECEIVING = "receiving"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# ---------------------------------------------------------------- Live Session States
class LiveSessionStatus(StrEnum):
    RESOLVING = "resolving"
    INGESTING = "ingesting"
    BUFFERING = "buffering"
    ANALYZING = "analyzing"
    DRAINING = "draining"
    TERMINATED = "terminated"
    ERROR = "error"


# ---------------------------------------------------------------- Transition Helpers
def validate_transition(entity: str, current: str, target: str, allowed_map: dict[str, set[str]]) -> None:
    """Validate that transition from current to target is permitted."""
    allowed = allowed_map.get(current, set())
    if target not in allowed and target != current:  # Idempotent re-entry is permitted
        raise InvalidStateTransitionError(entity, current, target, allowed)


def transition_job_status(job: dict[str, Any], target: str) -> str:
    """Safely transitions a job's status."""
    current = job.get("status", JobStatus.QUEUED)
    validate_transition(f"Job {job.get('id', '')}", str(current), str(target), JOB_TRANSITIONS)
    job["status"] = str(target)
    return str(target)


def transition_clip_status(clip: dict[str, Any], target: str) -> str:
    """Safely transitions a clip's status."""
    current = clip.get("status", ClipStatus.QUEUED)
    validate_transition(f"Clip {clip.get('id', '')}", str(current), str(target), CLIP_TRANSITIONS)
    clip["status"] = str(target)
    return str(target)
