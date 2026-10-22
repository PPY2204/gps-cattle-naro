from pathlib import Path

from device.session import SessionManager
from domain.enums import MountType
from domain.models import SubjectProfile
from services.recorder import RecorderService


def make_recorder(tmp_path: Path) -> RecorderService:
    return RecorderService(
        session_mgr=SessionManager(
            device_id="TEST-PI",
            subject_id="TEST-001",
            subject_type=SubjectProfile.cow_default().subject_type,
            mount_type=MountType.COLLAR,
        ),
        profile=SubjectProfile.cow_default(),
        raw_dir=tmp_path / "raw",
        processed_dir=tmp_path / "processed",
        enable_sqlite=False,
        is_recording=True,
    )


def test_recording_starts_automatically_and_start_is_idempotent(tmp_path: Path):
    recorder = make_recorder(tmp_path)
    try:
        session_id = recorder.session_mgr.session_id
        assert recorder.is_recording
        assert recorder.recording_state == "RECORDING"
        assert recorder.start_recording() == session_id
        assert recorder.session_mgr.session_id == session_id
    finally:
        recorder.stop_recording()


def test_stop_then_start_creates_a_new_session(tmp_path: Path):
    recorder = make_recorder(tmp_path)
    first_session = recorder.session_mgr.session_id
    recorder.stop_recording()

    second_session = recorder.start_new_session()

    try:
        assert second_session != first_session
        assert recorder.is_recording
        assert recorder.recording_state == "RECORDING"
    finally:
        recorder.stop_recording()
