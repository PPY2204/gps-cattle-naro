"""Raw NMEA sentence stream store."""
from pathlib import Path
from typing import TextIO, Union


class RawStore:
    """Appends raw NMEA lines directly to disk for 100% loss-free auditability."""

    def __init__(self, filepath: Union[str, Path]):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = open(self.filepath, "a", encoding="utf-8", buffering=1)

    def write_raw(self, raw_sentence: str) -> None:
        """Write raw line followed by newline."""
        line = raw_sentence.rstrip("\r\n")
        self._file.write(line + "\n")

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()
