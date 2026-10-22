"""Builder converting synchronized epoch bundles into unified GPSMeasurement models."""
import math
from datetime import datetime
from typing import Optional
from domain.enums import FixQuality
from domain.models import GPSMeasurement
from pipeline.synchronizer import EpochBundle


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points on Earth in meters."""
    r_earth = 6371000.0  # Earth radius in meters

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r_earth * c


class MeasurementBuilder:
    """Constructs GPSMeasurement objects and maintains trajectory continuity."""

    def __init__(self):
        self._last_measurement: Optional[GPSMeasurement] = None

    def build(self, bundle: EpochBundle) -> GPSMeasurement:
        """Construct a GPSMeasurement from an EpochBundle."""
        lat = bundle.gga.latitude if bundle.gga else (bundle.rmc.latitude if bundle.rmc else None)
        lon = bundle.gga.longitude if bundle.gga else (bundle.rmc.longitude if bundle.rmc else None)
        altitude = bundle.gga.altitude if bundle.gga else None
        fix_quality = bundle.gga.fix_quality if bundle.gga else FixQuality.INVALID
        satellites = bundle.gga.satellites if bundle.gga else 0
        hdop = bundle.gga.hdop if bundle.gga else None

        # Speed and track: prioritize VTG, fallback to RMC
        speed_kmh = None
        track_deg = None

        if bundle.vtg is not None:
            speed_kmh = bundle.vtg.speed_kmh
            track_deg = bundle.vtg.track_true or bundle.vtg.track_mag
        elif bundle.rmc is not None:
            speed_kmh = bundle.rmc.speed_kmh
            track_deg = bundle.rmc.track_degrees

        # Calculate incremental distance if consecutive points have coordinates
        dist_m = 0.0
        if (
            lat is not None
            and lon is not None
            and self._last_measurement is not None
            and self._last_measurement.latitude is not None
            and self._last_measurement.longitude is not None
        ):
            dist_m = haversine_distance(
                self._last_measurement.latitude,
                self._last_measurement.longitude,
                lat,
                lon,
            )

        measurement = GPSMeasurement(
            timestamp=bundle.arrival_time,
            utc_time=bundle.utc_time,
            latitude=lat,
            longitude=lon,
            altitude=altitude,
            fix_quality=fix_quality,
            satellites=satellites,
            hdop=hdop,
            speed_kmh=speed_kmh,
            track_degrees=track_deg,
            distance_from_previous_m=round(dist_m, 3),
            raw_sentences=list(bundle.raw_lines),
        )

        self._last_measurement = measurement
        return measurement
