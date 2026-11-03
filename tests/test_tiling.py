"""Every cell of a metro must have its whole 12 mile disc inside some tile.

Synthetic shapes stand in for Census boundaries so this runs offline; the
last test checks the committed config/metros.json the same way once it has
been generated from the real urban areas.
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import MultiPolygon, Point, Polygon, box

from runline.geo import distance_meters
from runline.grid import cell_centre, cell_half_diagonal_meters, cell_id
from runline.models import Coordinate, RoutePreferences
from runline.osm import graph_radius_meters
from runline.tiling import (
    assign_cells,
    metro_cells,
    metro_slug,
    needed_radius_meters,
    plan_tiles,
    uncovered_cells,
)
from scripts import build_metros

TWELVE_MILE_RADIUS = graph_radius_meters(RoutePreferences(12))


def _ellipse(latitude: float, longitude: float, width_km: float, height_km: float):
    circle = Point(longitude, latitude).buffer(1.0, quad_segs=32)
    return affinity.scale(
        circle,
        width_km / 2 / (111.32 * math.cos(math.radians(latitude))),
        height_km / 2 / 110.54,
    )


def _l_shape(latitude: float, longitude: float):
    """Concave, with a long thin arm: the case a naive bounding box misses."""

    degree_lon = 1 / (111.32 * math.cos(math.radians(latitude)))
    degree_lat = 1 / 110.54
    return Polygon(
        [
            (longitude, latitude),
            (longitude + 30 * degree_lon, latitude),
            (longitude + 30 * degree_lon, latitude + 3 * degree_lat),
            (longitude + 4 * degree_lon, latitude + 3 * degree_lat),
            (longitude + 4 * degree_lon, latitude + 25 * degree_lat),
            (longitude, latitude + 25 * degree_lat),
        ]
    )


def _islands(latitude: float, longitude: float):
    """A main body plus a far, small island, like a metro with a detached piece."""

    return MultiPolygon(
        [_ellipse(latitude, longitude, 20, 14), _ellipse(latitude + 0.25, longitude + 0.4, 1.5, 1.5)]
    )


SHAPES = {
    "l-shape-miami": _l_shape(25.7, -80.4),
    "islands-seattle": _islands(47.6, -122.3),
    "ellipse-anchorage": _ellipse(61.2, -149.9, 30, 18),
    "thin-sliver": box(-97.5, 32.7, -97.0, 32.703),
}


def test_needed_radius_is_the_planners_12_mile_crop() -> None:
    assert needed_radius_meters(12) == TWELVE_MILE_RADIUS
    assert TWELVE_MILE_RADIUS == pytest.approx(10_620, abs=10)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_every_cell_is_covered_at_the_12_mile_radius(name: str) -> None:
    plan = plan_tiles(SHAPES[name], name)

    assert len(plan.cells) > 0
    assert uncovered_cells(plan.cells, plan.tiles, TWELVE_MILE_RADIUS) == []


@pytest.mark.parametrize("name", ["l-shape-miami", "islands-seattle", "thin-sliver"])
def test_brute_force_every_point_of_the_shape_has_a_covering_tile(name: str) -> None:
    """Independent of metro_cells and the vectorised maths.

    Sample the shape every 60 m, map each sample to its grid cell with
    cell_id, and check that cell against the tiles with the scalar haversine
    the planner uses. Any cell touched by the shape must be covered.
    """

    shape = SHAPES[name]
    plan = plan_tiles(shape, name)
    min_lon, min_lat, max_lon, max_lat = shape.bounds
    step_lat = 60 / 110_540
    cells = set()
    latitude = min_lat
    while latitude <= max_lat:
        step_lon = 60 / (111_320 * math.cos(math.radians(latitude)))
        longitudes = np.arange(min_lon, max_lon, step_lon)
        for longitude in longitudes:
            if shape.contains(Point(longitude, latitude)):
                cells.add(cell_id(latitude, float(longitude), 200.0))
        latitude += step_lat

    assert cells <= set(plan.cells.ids())
    slack = cell_half_diagonal_meters(200.0) + TWELVE_MILE_RADIUS
    for cell in cells:
        centre = cell_centre(cell)
        assert any(
            distance_meters(Coordinate(tile.latitude, tile.longitude), centre) + slack <= tile.radius_meters
            for tile in plan.tiles
        ), cell


def test_each_cell_has_exactly_one_owner_within_its_reach() -> None:
    plan = plan_tiles(SHAPES["islands-seattle"], "islands")
    tiles = plan.tiles

    assert (plan.owner >= 0).all()
    owned = np.bincount(plan.owner, minlength=len(tiles))
    assert (owned > 0).all(), "a tile that owns no cell should have been dropped"
    for index in np.random.default_rng(0).choice(len(plan.cells), 500, replace=False):
        tile = tiles[plan.owner[index]]
        centre = Coordinate(plan.cells.latitudes[index], plan.cells.longitudes[index])
        assert distance_meters(Coordinate(tile.latitude, tile.longitude), centre) <= tile.reach_meters + 1e-6


def test_assignment_is_reproducible_from_the_stored_tiles() -> None:
    """The precompute regroups cells by tile from config alone."""

    plan = plan_tiles(SHAPES["ellipse-anchorage"], "anchorage")
    again = assign_cells(
        plan.cells.latitudes,
        plan.cells.longitudes,
        np.array([tile.latitude for tile in plan.tiles]),
        np.array([tile.longitude for tile in plan.tiles]),
        np.array([tile.reach_meters for tile in plan.tiles]),
    )
    assert (again == plan.owner).all()


def test_wider_core_means_fewer_bigger_tiles() -> None:
    shape = _ellipse(40.8, -73.9, 60, 40)
    narrow = plan_tiles(shape, "n", core_meters=2_000)
    wide = plan_tiles(shape, "w", core_meters=5_000)

    assert len(wide.tiles) < len(narrow.tiles) / 3
    assert wide.tiles[0].radius_meters > narrow.tiles[0].radius_meters
    assert uncovered_cells(wide.cells, wide.tiles, TWELVE_MILE_RADIUS) == []


def test_metro_cells_includes_cells_that_only_touch_the_boundary() -> None:
    # A 50 m square inside one cell has no cell centre inside it.
    centre = cell_centre(cell_id(38.03, -78.48, 200.0))
    tiny = Point(centre.longitude + 0.0008, centre.latitude + 0.0007).buffer(0.0002)
    assert cell_id(centre.latitude, centre.longitude, 200.0) in metro_cells(tiny).ids()


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("New York--Jersey City--Newark, NY--NJ", "new-york-ny"),
        ("Washington--Arlington, DC--VA--MD", "washington-dc"),
        ("Urban Honolulu, HI", "urban-honolulu-hi"),
        ("Charlottesville, VA", "charlottesville-va"),
    ],
)
def test_metro_slug(name: str, slug: str) -> None:
    assert metro_slug(name) == slug


# --- generator, end to end on a fake Census shapefile -----------------------

def _fake_census(tmp_path: Path) -> Path:
    geopandas = pytest.importorskip("geopandas")
    frame = geopandas.GeoDataFrame(
        {
            "UACE20": ["00001", "00002", "00003", "00004"],
            "GEOID20": ["00001", "00002", "00003", "00004"],
            "NAME20": ["Big City--Other, TX--OK", "Mid Town, VA", "Small Place, OH", "Island City, PR"],
        },
        geometry=[
            _ellipse(32.8, -97.0, 30, 20),
            _islands(38.0, -78.5),
            _ellipse(40.0, -83.0, 6, 6),
            _ellipse(18.4, -66.1, 20, 10),
        ],
        crs=4326,
    )
    shapefile_dir = tmp_path / "shp"
    shapefile_dir.mkdir()
    frame.to_file(shapefile_dir / "tl_2024_us_uac20.shp")
    archive = tmp_path / "tl_2024_us_uac20.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for part in shapefile_dir.iterdir():
            bundle.write(part, part.name)
    return archive


POPULATION_FILES = {
    "api.json": json.dumps(
        [
            ["NAME", "P1_001N", "urban area"],
            ["Big City--Other, TX--OK Urban Area", "5000000", "00001"],
            ["Mid Town, VA Urban Area", "800000", "00002"],
            ["Small Place, OH Urban Area", "60000", "00003"],
            ["Island City, PR Urban Area", "9000000", "00004"],
        ]
    ),
    "list.csv": "GEOID20,NAME20,POP20\n00001,x,5000000\n00002,y,800000\n00003,z,60000\n00004,w,9000000\n",
    "register.txt": (
        "                       Urban area          Population    Housing   (square miles)\n"
        "Big City--Other, TX--OK.................   5,000,000   2,000,000   1,000.0\n"
        "Island City, PR.........................   9,000,000   3,000,000     100.0\n"
        "Mid Town, VA............................     800,000     300,000     100.0\n"
        "Small Place, OH.........................      60,000      20,000      10.0\n"
    ),
}


@pytest.mark.parametrize("population_name", sorted(POPULATION_FILES))
def test_generator_writes_a_config_that_passes_its_own_check(tmp_path: Path, population_name: str) -> None:
    archive = _fake_census(tmp_path)
    population = tmp_path / population_name
    population.write_text(POPULATION_FILES[population_name], encoding="utf-8")
    output = tmp_path / "metros.json"
    boundaries = tmp_path / "metro-boundaries.geojson"

    status = build_metros.main(
        ["--urban-areas", str(archive), "--population", str(population), "--top", "2",
         "--output", str(output), "--boundaries", str(boundaries)]
    )

    assert status == 0
    config = json.loads(output.read_text(encoding="utf-8"))
    # Puerto Rico is outside the US extract, and only the top two remain.
    assert [metro["slug"] for metro in config["metros"]] == ["big-city-tx", "mid-town-va"]
    tile = config["metros"][0]["tiles"][0]
    assert {"slug", "label", "latitude", "longitude", "radius_meters"} <= set(tile)
    assert sum(t["cells"] for t in config["metros"][0]["tiles"]) == config["metros"][0]["cells"]
    assert build_metros.check(output, boundaries) == []


def test_check_reports_a_missing_tile(tmp_path: Path) -> None:
    archive = _fake_census(tmp_path)
    population = tmp_path / "list.csv"
    population.write_text(POPULATION_FILES["list.csv"], encoding="utf-8")
    output, boundaries = tmp_path / "metros.json", tmp_path / "metro-boundaries.geojson"
    build_metros.main(["--urban-areas", str(archive), "--population", str(population), "--top", "1",
                       "--output", str(output), "--boundaries", str(boundaries)])

    config = json.loads(output.read_text(encoding="utf-8"))
    config["metros"][0]["tiles"].pop(len(config["metros"][0]["tiles"]) // 2)
    output.write_text(json.dumps(config), encoding="utf-8")

    problems = build_metros.check(output, boundaries)
    assert problems and "uncovered" in problems[0]


def test_stored_boundary_contains_the_census_polygon() -> None:
    census = _islands(38.0, -78.5)
    stored = build_metros.stored_boundary(census)
    assert stored.contains(census)


# --- the committed config ---------------------------------------------------

def test_committed_metros_cover_every_cell_at_the_12_mile_radius() -> None:
    if not build_metros.METROS_PATH.exists():
        pytest.skip("config/metros.json not generated yet; see scripts/build_metros.py")
    assert build_metros.check(build_metros.METROS_PATH, build_metros.BOUNDARIES_PATH) == []
