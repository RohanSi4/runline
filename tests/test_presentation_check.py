"""The rehearsal must reject broken exports, rather than call them demo ready."""

import copy
import xml.etree.ElementTree as ET

import pytest

from runline.export import candidate_feature
from runline.gpx import candidate_to_gpx
from runline.models import Coordinate, RouteCandidate, RouteMetrics
from scripts.presentation_check import check_routes


def payload():
    candidate = RouteCandidate(
        "example", (1, 2, 3, 1),
        (Coordinate(10, 20), Coordinate(10.01, 20), Coordinate(10, 20.01), Coordinate(10, 20)),
        RouteMetrics(3, 100, 1, 1, 0.2, 0.0),
    )
    return {"source": "pool", "routes": [{"feature": candidate_feature(candidate),
                                          "gpx": candidate_to_gpx(candidate)}]}


def test_rehearsal_accepts_matching_closed_map_and_gpx():
    result = check_routes(payload(), 3)
    assert result["routes"] == result["in_band"] == 1
    assert result["source"] == "pool"


def test_rehearsal_rejects_an_open_map_route():
    broken = copy.deepcopy(payload())
    broken["routes"][0]["feature"]["geometry"]["coordinates"][-1] = [20.02, 10]
    with pytest.raises(ValueError, match="finish where it starts"):
        check_routes(broken, 3)


def test_rehearsal_rejects_a_gpx_that_disagrees_with_the_map():
    broken = payload()
    track = ET.fromstring(broken["routes"][0]["gpx"])
    track.findall(".//{*}trkpt")[1].set("lat", "10.02")
    broken["routes"][0]["gpx"] = ET.tostring(track, encoding="unicode")
    with pytest.raises(ValueError, match="differs from the displayed"):
        check_routes(broken, 3)
