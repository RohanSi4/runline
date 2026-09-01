from __future__ import annotations

import argparse
from pathlib import Path

from .export import export_candidates
from .models import (
    Coordinate,
    ElevationPreference,
    RoutePreferences,
    SurfacePreference,
)
from .planner import geocode_address, plan_routes


def _coordinate(args: argparse.Namespace) -> Coordinate:
    if args.address:
        return geocode_address(args.address)
    if args.latitude is None or args.longitude is None:
        raise SystemExit("provide --address or both --latitude and --longitude")
    return Coordinate(args.latitude, args.longitude)


def _generate(args: argparse.Namespace) -> int:
    origin = _coordinate(args)
    preferences = RoutePreferences(
        target_distance_miles=args.distance,
        drive_radius_miles=args.drive_radius,
        surface=SurfacePreference(args.surface),
        elevation=ElevationPreference(args.elevation),
        result_count=args.results,
    )
    cache_directory = Path(args.cache)
    result = plan_routes(
        origin,
        preferences,
        cache_directory=cache_directory,
        elevation_source=args.elevation_source,
        start_candidate_limit=args.start_candidates,
    )
    candidates = result.candidates
    if preferences.drive_radius_miles:
        print(
            f"Comparing start here with {result.discovered_starts} public drive-to starts"
        )
    for warning in result.warnings:
        print(f"Warning: {warning}")
    if result.fetched_elevation_cells:
        print(
            f"Fetched {result.fetched_elevation_cells} elevation cells from "
            "Open-Meteo/Copernicus DEM"
        )
    if not candidates:
        raise SystemExit("no viable loops found; try a different distance or surface preference")
    export_candidates(candidates, Path(args.output))
    print(f"Generated {len(candidates)} route options in {args.output}")
    for index, candidate in enumerate(candidates, start=1):
        metrics = candidate.metrics
        elevation = (
            f"{metrics.elevation_gain_feet:.0f} ft gain"
            if metrics.elevation_coverage >= 0.95
            else "elevation unavailable"
        )
        print(
            f"{index}: {candidate.start.label if candidate.start else 'Start here'}, "
            f"{metrics.drive_distance_miles:.1f} mi drive, "
            f"{metrics.distance_miles:.2f} mi, "
            f"{elevation}, "
            f"{metrics.traffic_signal_events} signals, "
            f"{metrics.trail_fraction:.0%} trail, "
            f"{metrics.repeated_fraction:.0%} repeated"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runroute")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate and score running loops")
    generate.add_argument("--address")
    generate.add_argument("--latitude", type=float)
    generate.add_argument("--longitude", type=float)
    generate.add_argument("--distance", type=float, required=True)
    generate.add_argument("--drive-radius", type=float, choices=(0, 1, 3, 5), default=0)
    generate.add_argument("--surface", choices=[item.value for item in SurfacePreference], default="mixed")
    generate.add_argument("--elevation", choices=[item.value for item in ElevationPreference], default="balanced")
    generate.add_argument(
        "--elevation-source", choices=("open-meteo", "none"), default="open-meteo"
    )
    generate.add_argument("--results", type=int, default=3)
    generate.add_argument("--start-candidates", type=int, default=5)
    generate.add_argument("--cache", default="cache")
    generate.add_argument("--output", default="output/latest")
    generate.set_defaults(handler=_generate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
