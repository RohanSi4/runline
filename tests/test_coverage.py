import gzip
import json
import pickle
from pathlib import Path

import networkx as nx
import pytest

from runline import coverage
from runline.coverage import Area, CoverageError
from runline.models import Coordinate
from runline.osm import load_graph


def build_graphs_dir(tmp_path: Path) -> Path:
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=-78.4767, y=38.0293)
    (tmp_path / "cville.pkl.gz").write_bytes(gzip.compress(pickle.dumps(graph)))
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "areas": [
                    {
                        "slug": "cville",
                        "label": "Charlottesville, VA",
                        "latitude": 38.0293,
                        "longitude": -78.4767,
                        "radius_meters": 12000,
                        "file": "cville.pkl.gz",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def graphs_dir(tmp_path, monkeypatch):
    directory = build_graphs_dir(tmp_path)
    monkeypatch.setenv("RUNLINE_GRAPHS_DIR", str(directory))
    coverage._read_manifest.cache_clear()
    coverage._load_graph_file.cache_clear()
    yield directory
    coverage._read_manifest.cache_clear()
    coverage._load_graph_file.cache_clear()


def test_area_covers_only_when_the_whole_disc_fits() -> None:
    area = Area("a", "A", Coordinate(38.0, -78.0), 10_000, "a.pkl.gz")

    assert area.covers(Coordinate(38.0, -78.0), 9_000)
    # Centre offset plus request radius exceeds the area radius.
    assert not area.covers(Coordinate(38.05, -78.0), 9_000)
    assert not area.covers(Coordinate(38.0, -78.0), 11_000)


def test_find_area_reads_the_manifest(graphs_dir) -> None:
    found = coverage.find_area(Coordinate(38.03, -78.48), 2_655)

    assert found is not None
    assert found.label == "Charlottesville, VA"
    assert coverage.area_labels() == ("Charlottesville, VA",)


def test_find_area_returns_none_outside_coverage(graphs_dir) -> None:
    assert coverage.find_area(Coordinate(40.7128, -74.0060), 2_655) is None


def test_load_graph_prefers_the_precomputed_area(graphs_dir, tmp_path) -> None:
    graph = load_graph(Coordinate(38.03, -78.48), 2_655, tmp_path / "cache")

    assert graph.number_of_nodes() == 1


def test_load_graph_refuses_to_download_when_disabled(graphs_dir, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

    with pytest.raises(CoverageError) as error:
        load_graph(Coordinate(40.7128, -74.0060), 2_655, tmp_path / "cache")

    assert "Charlottesville, VA" in str(error.value)


def test_downloads_are_disabled_on_vercel(monkeypatch) -> None:
    monkeypatch.delenv("RUNLINE_ALLOW_OSM_DOWNLOAD", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    assert coverage.downloads_allowed() is False

    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "1")
    assert coverage.downloads_allowed() is True


def test_missing_manifest_yields_no_areas(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_GRAPHS_DIR", str(tmp_path / "absent"))
    coverage._read_manifest.cache_clear()

    assert coverage.areas() == ()
    coverage._read_manifest.cache_clear()


def test_message_distinguishes_distance_from_location(graphs_dir) -> None:
    inside = coverage.unsupported_message(Coordinate(38.03, -78.48), 30_000)
    assert "too long" in inside
    assert "Charlottesville, VA" in inside

    outside = coverage.unsupported_message(Coordinate(40.7128, -74.0060), 2_655)
    assert "no map data for that location" in outside
    assert "Supported areas" in outside


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self._payload)


def test_geocode_calls_nominatim_directly() -> None:
    """Geocoding must not need osmnx, which drags in the whole geo stack."""

    from runline.planner import NOMINATIM_URL, geocode_address

    session = _FakeSession([{"lat": "38.0293", "lon": "-78.4767"}])
    result = geocode_address("Charlottesville, VA", session=session)

    assert (round(result.latitude, 4), round(result.longitude, 4)) == (38.0293, -78.4767)
    url, kwargs = session.calls[0]
    assert url == NOMINATIM_URL
    # Nominatim's usage policy requires an identifying User-Agent.
    assert "runline" in kwargs["headers"]["User-Agent"]


def test_geocode_reports_an_unknown_address() -> None:
    from runline.planner import geocode_address

    with pytest.raises(ValueError):
        geocode_address("nowhere at all", session=_FakeSession([]))
