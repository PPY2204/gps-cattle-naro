"""Serial line reader for GPS hardware via RFCOMM or USB/UART."""
import logging
from typing import Generator, Optional
import serial
from domain.exceptions import DeviceConnectionError

logger = logging.getLogger(__name__)


class SerialReader:
    """Reads raw NMEA lines from serial stream or Bluetooth RFCOMM TTY."""

    def __init__(
        self,
        port: str = "/dev/rfcomm0",
        baud_rate: int = 115200,
        timeout: float = 2.0,
        rate_hz: float | None = None,
        configure_rate: bool = True,
    ):
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.rate_hz = rate_hz
        self.configure_rate = configure_rate
        self._serial: Optional[serial.Serial] = None

    @staticmethod
    def _ubx_rate_packet(rate_hz: float) -> bytes:
        if rate_hz <= 0 or 1000.0 / rate_hz > 65535:
            raise ValueError("GPS rate must be positive and no faster than 1000 Hz")
        measurement_period_ms = round(1000.0 / rate_hz)
        payload = (
            measurement_period_ms.to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
        )
        body = bytes((0x06, 0x08)) + len(payload).to_bytes(2, "little") + payload
        ck_a = 0
        ck_b = 0
        for byte in body:
            ck_a = (ck_a + byte) & 0xFF
            ck_b = (ck_b + ck_a) & 0xFF
        return b"\xB5\x62" + body + bytes((ck_a, ck_b))

    def open(self) -> None:
        """Open the serial port connection with DTR/RTS enabled for Bluetooth SPP."""
        try:
            logger.info(f"Opening serial connection to {self.port} at {self.baud_rate} baud...")
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baud_rate,
                timeout=self.timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # Crucial for Bluetooth RFCOMM / virtual TTY devices:
            try:
                self._serial.dtr = True
                self._serial.rts = True
            except Exception:
                pass
            if self.configure_rate and self.rate_hz is not None:
                packet = self._ubx_rate_packet(self.rate_hz)
                self._serial.write(packet)
                self._serial.flush()
                logger.info("Requested GNSS output rate %.3f Hz", self.rate_hz)
        except serial.SerialException as e:
            raise DeviceConnectionError(f"Failed to open {self.port}: {e}") from e

    def read_lines(self) -> Generator[str, None, None]:
        """Generator yielding decoded NMEA sentences."""
        if not self._serial or not self._serial.is_open:
            self.open()

        assert self._serial is not None
        while self._serial.is_open:
            try:
                line_bytes = self._serial.readline()
                if not line_bytes:
                    continue
                try:
                    line = line_bytes.decode("ascii", errors="replace").strip()
                except Exception:
                    continue

                if line.startswith(("$", "!")):
                    yield line
            except serial.SerialException as e:
                logger.error(f"Serial read error on {self.port}: {e}")
                raise DeviceConnectionError(f"Lost connection to {self.port}: {e}") from e

    def close(self) -> None:
        """Close serial connection cleanly."""
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.warning(f"Error closing serial port: {e}")
            finally:
                self._serial = None
