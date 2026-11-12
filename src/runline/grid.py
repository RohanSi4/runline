"""The global grid of start cells that route pools are keyed by.

Ids look like ``200:<row>:<column>`` and depend only on the point and the grid
size, never on an area or tile, so the server can find a request's pool from
its start point alone and two overlapping tiles agree on every cell.
"""

from __future__ import annotations

import math

from .models import Coordinate

DEFAULT_GRID_METERS = 200.0
METERS_PER_DEGREE_LATITUDE = 110_540.0
METERS_PER_DEGREE_LONGITUDE = 111_320.0


def row_height_degrees(grid_meters: float) -> float:
    return grid_meters / METERS_PER_DEGREE_LATITUDE


def row_centre_latitude(row: int, grid_meters: float) -> float:
    return (row + 0.5) * row_height_degrees(grid_meters)


def column_width_degrees(row: int, grid_meters: float) -> float:
    """Columns are sized at the row's centre latitude, so cells stay square."""

    return grid_meters / (
        METERS_PER_DEGREE_LONGITUDE * math.cos(math.radians(row_centre_latitude(row, grid_meters)))
    )


def cell_row(latitude: float, grid_meters: float) -> int:
    return math.floor(latitude / row_height_degrees(grid_meters))


def cell_id(latitude: float, longitude: float, grid_meters: float) -> str:
    """Global grid cell containing a point, the same for every area."""

    row = cell_row(latitude, grid_meters)
    column = math.floor(longitude / column_width_degrees(row, grid_meters))
    return f"{int(grid_meters)}:{row}:{column}"


def cell_centre(cell: str) -> Coordinate:
    grid, row, column = (int(part) for part in cell.split(":"))
    return Coordinate(
        row_centre_latitude(row, grid),
        (column + 0.5) * column_width_degrees(row, grid),
    )


def cell_half_diagonal_meters(grid_meters: float) -> float:
    """Farthest a point inside a cell can be from its centre.

    The pool's origin is the walkable node nearest the cell centre, and a cell
    only gets a pool if it holds a node, so the origin is never farther than
    this from the centre. The 0.5% covers the grid's flat-earth constants
    against haversine.
    """

    return grid_meters * math.sqrt(2) / 2 * 1.005


def neighbouring_cells(cell: str, radius: int = 1) -> list[str]:
    """The cell and those up to ``radius`` rows and columns away, the cell itself first.

    With the default radius that is the eight around it; radius 2 adds the
    sixteen around those. Steps run 0, -1, 1, -2, 2, ... in rows and in
    columns. Rows differ slightly in column
    width, so the rows above and below are found by point rather than by
    adding to the column.
    """

    grid, row, _ = (int(part) for part in cell.split(":"))
    centre = cell_centre(cell)
    width = column_width_degrees(row, grid)
    steps = sorted(range(-radius, radius + 1), key=lambda step: (abs(step), step))
    cells = [cell]
    for row_step in steps:
        latitude = row_centre_latitude(row + row_step, grid)
        for column_step in steps:
            neighbour = cell_id(latitude, centre.longitude + column_step * width, grid)
            if neighbour not in cells:
                cells.append(neighbour)
    return cells
