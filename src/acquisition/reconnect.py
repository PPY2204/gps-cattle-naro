"""Resilient reconnection loop for field IoT GPS acquisition."""
import logging
import time
from typing import Callable, Generator, TypeVar

from acquisition.serial_reader import SerialReader
from domain.exceptions import DeviceConnectionError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ResilientStreamReader:
    """Wraps SerialReader with automatic retry and backoff when connection drops."""

    def __init__(
        self,
        port: str = "/dev/rfcomm0",
        baud_rate: int = 115200,
        timeout: float = 2.0,
        reconnect_delay_seconds: float = 3.0,
        rate_hz: float | None = None,
        configure_rate: bool = True,
        bluetooth_mac: str | None = None,
        rfcomm_index: int = 0,
        rfcomm_channel: int = 1,
        max_retries: int = -1,  # -1 for infinite retries in field
        on_reconnect_callback: Callable[[], None] | None = None,
    ):
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay_seconds
        self.rate_hz = rate_hz
        self.configure_rate = configure_rate
        self.bluetooth_mac = bluetooth_mac
        self.rfcomm_index = rfcomm_index
        self.rfcomm_channel = rfcomm_channel
        self.max_retries = max_retries
        self.on_reconnect = on_reconnect_callback
        self._running = False
        self._reader: SerialReader | None = None

    def stream_lines(self) -> Generator[str, None, None]:
        """Continuously yield lines across reconnect cycles."""
        self._running = True
        attempt = 0

        while self._running:
            try:
                # If /dev/rfcomm0 does not exist, try auto-binding to DG-PRO1S if on Linux
                import os, subprocess
                if (
                    self.bluetooth_mac
                    and not os.path.exists(self.port)
                    and "rfcomm" in self.port
                ):
                    logger.info(f"{self.port} not found, attempting auto-bind...")
                    try:
                        subprocess.run(
                            [
                                "sudo",
                                "-n",
                                "rfcomm",
                                "bind",
                                str(self.rfcomm_index),
                                self.bluetooth_mac,
                                str(self.rfcomm_channel),
                            ],
                            timeout=5,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    except Exception:
                        pass

                self._reader = SerialReader(
                    self.port,
                    self.baud_rate,
                    self.timeout,
                    rate_hz=self.rate_hz,
                    configure_rate=self.configure_rate,
                )
                logger.info(f"Connecting to GNSS on {self.port}...")
                for line in self._reader.read_lines():
                    if not self._running:
                        break
                    yield line
                attempt = 0
            except DeviceConnectionError as e:
                attempt += 1
                logger.warning(f"Connection lost ({e}). Attempt {attempt} to reconnect...")
                if self._reader:
                    self._reader.close()
                if self.on_reconnect:
                    try:
                        self.on_reconnect()
                    except Exception as cb_err:
                        logger.warning(f"Error in reconnect callback: {cb_err}")

                if 0 <= self.max_retries < attempt:
                    logger.error(f"Max retries ({self.max_retries}) reached. Exiting.")
                    break

                time.sleep(self.reconnect_delay)
            except Exception as e:
                logger.exception(f"Unexpected error in reader stream: {e}")
                time.sleep(self.reconnect_delay)

    def stop(self) -> None:
        """Signal the stream loop to stop."""
        self._running = False
        if self._reader:
            self._reader.close()
