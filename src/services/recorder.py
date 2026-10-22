"""Recorder service orchestrating live acquisition, processing, and multi-store persistence."""
import logging
import threading
import time
from pathlib import Path
from typing import Generator, Optional

from acquisition.reconnect import ResilientStreamReader
from device.health import HealthMonitor
from device.session import SessionManager
from domain.models import GPSMeasurement, SubjectProfile
from pipeline.filter import KinematicFilter
from pipeline.measurement import MeasurementBuilder
from pipeline.synchronizer import EpochBundle, EpochSynchronizer
from pipeline.validator import QualityValidator
from protocol.gga import GGAData
from protocol.nmea import NMEARouter
from protocol.rmc import RMCData
from protocol.vtg import VTGData
from storage.csv_store import CSVStore
from storage.raw_store import RawStore
from storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class RecorderService:
    """Core runtime engine executing on the Raspberry Pi Zero 2 W."""

    def __init__(
        self,
        session_mgr: SessionManager,
        profile: SubjectProfile,
        raw_dir: Path = Path("data/raw"),
        processed_dir: Path = Path("data/processed"),
        min_satellites: int = 4,
        max_hdop: float = 5.0,
        enable_sqlite: bool = True,
        enable_raw: bool = True,
        on_measurement: Optional[object] = None,
        on_raw_line: Optional[object] = None,
        is_recording: bool = True,
    ):
        self.session_mgr = session_mgr
        self.profile = profile
        self.health = HealthMonitor()
        self.on_measurement = on_measurement
        self.on_raw_line = on_raw_line
        self.latest_measurement: Optional[GPSMeasurement] = None
        self.raw_dir = raw_dir
        self.processed_dir = processed_dir
        self.enable_raw = enable_raw
        self.enable_sqlite = enable_sqlite
        self._state_lock = threading.RLock()
        self._is_recording = False
        self._last_flush = time.monotonic()
        self._measurements_since_flush = 0

        # Pipeline components
        self.router = NMEARouter(strict_checksum=False)
        self.synchronizer = EpochSynchronizer()
        self.builder = MeasurementBuilder()
        self.validator = QualityValidator(
            min_satellites=min_satellites,
            max_hdop=max_hdop,
            require_fix=True,
        )
        self.filter = KinematicFilter(profile=self.profile)

        self.raw_store: Optional[RawStore] = None
        self.csv_store: Optional[CSVStore] = None
        self.sqlite_store: Optional[SQLiteStore] = None

        if is_recording:
            self.start_recording()

    @property
    def is_recording(self) -> bool:
        with self._state_lock:
            return self._is_recording

    @property
    def recording_state(self) -> str:
        return "RECORDING" if self.is_recording else "STOPPED"

    def _init_stores(self) -> None:
        if self.csv_store or self.raw_store or self.sqlite_store:
            return
        session_id = self.session_mgr.session_id
        sub_dir = self.session_mgr.subject_dir
        d_str = self.session_mgr.date_str
        device_dir = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in self.session_mgr.metadata.device_id
        )

        raw_file = self.raw_dir / device_dir / sub_dir / d_str / f"{session_id}_raw.nmea"
        csv_file = self.processed_dir / device_dir / sub_dir / d_str / f"{session_id}.csv"
        db_file = self.processed_dir / device_dir / sub_dir / d_str / f"{session_id}.db"

        self.raw_store = RawStore(raw_file) if self.enable_raw else None
        self.csv_store = CSVStore(csv_file)
        self.sqlite_store = SQLiteStore(db_file) if self.enable_sqlite else None
        self._last_flush = time.monotonic()
        self._measurements_since_flush = 0

    def start_recording(self) -> str:
        """Start recording once; repeated calls are idempotent."""
        with self._state_lock:
            if self._is_recording:
                return self.session_mgr.session_id
            self._init_stores()
            self._is_recording = True
            return self.session_mgr.session_id

    def start_new_session(self, subject_id: Optional[str] = None) -> str:
        """Close current stores and roll to a new timestamped session."""
        with self._state_lock:
            if self._is_recording:
                return self.session_mgr.session_id
            return self._create_next_session(subject_id)

    def rotate_session(self) -> str:
        """Close the current session and immediately start a fresh one."""
        with self._state_lock:
            remaining = self.synchronizer.flush()
            if remaining is not None:
                self.handle_epoch(remaining)
            self.stop_recording()
            return self._create_next_session()

    def _create_next_session(self, subject_id: Optional[str] = None) -> str:
        self.close()
        sub_id = subject_id or self.session_mgr.metadata.subject_id
        self.synchronizer = EpochSynchronizer()
        self.builder = MeasurementBuilder()
        self.session_mgr = SessionManager(
            device_id=self.session_mgr.metadata.device_id,
            subject_id=sub_id,
            subject_type=self.profile.subject_type,
            mount_type=self.session_mgr.metadata.mount_type,
            sampling_rate_hz=self.session_mgr.metadata.sampling_rate_hz,
        )
        return self.start_recording()

    def switch_profile(self, profile: SubjectProfile) -> str:
        """Persist a new profile in the next recording session."""
        with self._state_lock:
            was_recording = self._is_recording
            if was_recording:
                self.stop_recording()
            self.profile = profile
            self.filter = KinematicFilter(profile=self.profile)
            self.session_mgr = SessionManager(
                device_id=self.session_mgr.metadata.device_id,
                subject_id=self.session_mgr.metadata.subject_id,
                subject_type=profile.subject_type,
                mount_type=self.session_mgr.metadata.mount_type,
                sampling_rate_hz=self.session_mgr.metadata.sampling_rate_hz,
            )
            if was_recording:
                return self.start_recording()
            return self.session_mgr.session_id

    def stop_recording(self) -> None:
        """Stop recording and flush stores."""
        with self._state_lock:
            self._is_recording = False
            self.flush()
            self.close()

    def process_sentence(self, sentence: str) -> Optional[EpochBundle]:
        """Process a single raw NMEA sentence through router and synchronizer."""
        self.health.record_sentence(sentence)
        with self._state_lock:
            if self.raw_store and self._is_recording:
                self.raw_store.write_raw(sentence)

        if self.on_raw_line:
            try:
                self.on_raw_line(sentence)
            except Exception:
                pass

        try:
            sent_type, parsed = self.router.route(sentence)
        except Exception as e:
            logger.debug(f"Router skipped line: {e}")
            return None

        emitted_bundle = None
        if sent_type == "GGA" and isinstance(parsed, GGAData):
            emitted_bundle = self.synchronizer.feed_gga(parsed, sentence)
        elif sent_type == "VTG" and isinstance(parsed, VTGData):
            self.synchronizer.feed_vtg(parsed, sentence)
        elif sent_type == "RMC" and isinstance(parsed, RMCData):
            emitted_bundle = self.synchronizer.feed_rmc(parsed, sentence)

        return emitted_bundle

    def handle_epoch(self, bundle: EpochBundle) -> None:
        """Construct measurement, validate, filter, and write to stores."""
        measurement = self.builder.build(bundle)
        measurement.quality = self.validator.validate(measurement)
        measurement = self.filter.filter(measurement)

        with self._state_lock:
            if self._is_recording:
                if self.csv_store:
                    self.csv_store.write_measurement(measurement)
                if self.sqlite_store:
                    self.sqlite_store.write_measurement(measurement)
                self._measurements_since_flush += 1
                if (
                    self._measurements_since_flush >= 10
                    or time.monotonic() - self._last_flush >= 1.0
                ):
                    self.flush()

        self.health.record_measurement(measurement)
        self.latest_measurement = measurement

        if self.on_measurement:
            try:
                self.on_measurement(measurement)
            except Exception as e:
                logger.debug(f"Error in on_measurement callback: {e}")

    def run_stream(self, line_generator: Generator[str, None, None]) -> None:
        """Run continuous stream loop."""
        try:
            for line in line_generator:
                bundle = self.process_sentence(line)
                if bundle is not None:
                    self.handle_epoch(bundle)
        finally:
            # Flush final buffered bundle
            remaining = self.synchronizer.flush()
            if remaining is not None:
                self.handle_epoch(remaining)

    def close(self) -> None:
        """Cleanly close all stores."""
        with self._state_lock:
            if self.raw_store:
                self.raw_store.close()
                self.raw_store = None
            if self.csv_store:
                self.csv_store.close()
                self.csv_store = None
            if self.sqlite_store:
                self.sqlite_store.close()
                self.sqlite_store = None
            self._is_recording = False

    def flush(self) -> None:
        """Flush all active stores without closing the current session."""
        with self._state_lock:
            if self.raw_store:
                self.raw_store.flush()
            if self.csv_store:
                self.csv_store.flush()
            if self.sqlite_store:
                self.sqlite_store.flush()
            self._last_flush = time.monotonic()
            self._measurements_since_flush = 0
