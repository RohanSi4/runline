from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


METERS_PER_MILE = 1609.344


class SurfacePreference(StrEnum):
    ROAD = "road"
    MIXED = "mixed"
    TRAIL = "trail"


class ElevationPreference(StrEnum):
    FLAT = "flat"
    BALANCED = "balanced"
    HILLY = "hilly"


@dataclass(frozen=True)
class Coordinate:
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")


@dataclass(frozen=True)
class StartCandidate:
    label: str
    kind: str
    coordinate: Coordinate
    drive_distance_miles: float


@dataclass(frozen=True)
class RoutePreferences:
    target_distance_miles: float
    drive_radius_miles: float = 0
    surface: SurfacePreference = SurfacePreference.MIXED
    elevation: ElevationPreference = ElevationPreference.BALANCED
    distance_tolerance_miles: float = 0.25
    result_count: int = 3

    def __post_init__(self) -> None:
        if self.target_distance_miles <= 0:
            raise ValueError("target distance must be positive")
        if self.drive_radius_miles not in {0, 1, 3, 5}:
            raise ValueError("drive radius must be one of 0, 1, 3, or 5 miles")
        if self.distance_tolerance_miles <= 0:
            raise ValueError("distance tolerance must be positive")
        if self.result_count <= 0:
            raise ValueError("result count must be positive")

    @property
    def target_distance_meters(self) -> float:
        return self.target_distance_miles * METERS_PER_MILE


@dataclass(frozen=True)
class RouteMetrics:
    distance_miles: float
    elevation_gain_feet: float
    traffic_signal_events: int
    major_crossing_events: int
    trail_fraction: float
    repeated_fraction: float
    drive_distance_miles: float = 0
    elevation_coverage: float = 1

    def __post_init__(self) -> None:
        if self.distance_miles <= 0:
            raise ValueError("route distance must be positive")
        for name, value in (
            ("trail_fraction", self.trail_fraction),
            ("repeated_fraction", self.repeated_fraction),
            ("elevation_coverage", self.elevation_coverage),
        ):
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass
class RouteCandidate:
    route_id: str
    node_ids: tuple[int, ...]
    coordinates: tuple[Coordinate, ...]
    metrics: RouteMetrics
    start: StartCandidate | None = None
    score: float = field(default=float("inf"))
    score_components: dict[str, float] = field(default_factory=dict)
