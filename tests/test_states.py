"""Unit tests for studio.states (state definitions and transition validators)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.states import (
    ClipStatus,
    InvalidStateTransitionError,
    JobStage,
    JobStatus,
    ModelStatus,
    PostStatus,
    UploadStatus,
    VoiceoverMode,
    transition_clip_status,
    transition_job_status,
)


def main():
    # 1. StrEnum string compatibility
    assert JobStatus.QUEUED == "queued"
    assert JobStatus.RUNNING == "running"
    assert JobStatus.DONE == "done"
    assert JobStatus.ERROR == "error"
    assert ClipStatus.RENDERING == "rendering"
    assert VoiceoverMode.DUCK == "duck"
    print("PASS StrEnum string equality")

    # 2. JSON serialization
    data = {"status": JobStatus.RUNNING, "clip_status": ClipStatus.DONE}
    serialized = json.dumps(data)
    assert json.loads(serialized) == {"status": "running", "clip_status": "done"}
    print("PASS JSON serialization compatibility")

    # 3. Valid job transitions
    job = {"id": "test-job-1", "status": JobStatus.QUEUED}
    transition_job_status(job, JobStatus.RUNNING)
    assert job["status"] == "running"
    transition_job_status(job, JobStatus.DONE)
    assert job["status"] == "done"
    transition_job_status(job, JobStatus.QUEUED)
    assert job["status"] == "queued"
    print("PASS Valid job lifecycle transitions")

    # 4. Invalid job transition
    transition_job_status(job, JobStatus.DONE)
    try:
        transition_job_status(job, JobStatus.RUNNING)
        raise AssertionError("Should have raised InvalidStateTransitionError")
    except InvalidStateTransitionError:
        print("PASS Invalid job transition rejected")

    # 5. Valid clip transitions
    clip = {"id": "test-clip-1", "status": ClipStatus.QUEUED}
    transition_clip_status(clip, ClipStatus.RENDERING)
    assert clip["status"] == "rendering"
    transition_clip_status(clip, ClipStatus.DONE)
    assert clip["status"] == "done"
    transition_clip_status(clip, ClipStatus.QUEUED)
    assert clip["status"] == "queued"
    transition_clip_status(clip, ClipStatus.ERROR)
    assert clip["status"] == "error"
    print("PASS Valid clip lifecycle transitions")

    # 6. Invalid clip transition
    try:
        transition_clip_status(clip, ClipStatus.DONE)
        raise AssertionError("Should have raised InvalidStateTransitionError")
    except InvalidStateTransitionError:
        print("PASS Invalid clip transition rejected")

    # 7. Idempotent transition
    transition_job_status(job, JobStatus.DONE)
    assert job["status"] == "done"
    print("PASS Idempotent state re-entry")

    print("\nALL STATE TESTS PASSED")


if __name__ == "__main__":
    main()
