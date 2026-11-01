import json
from pathlib import Path

import pytest

from app.cli.import_buildings import normalize_feature, read_features


def sample_feature() -> dict:
    return {
        "type": "Feature",
        "id": "unstable-wfs-row-id",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[21.0, 52.2], [21.001, 52.2], [21.001, 52.201], [21.0, 52.2]]],
        },
        "properties": {"ID_BUDYNKU": "stable-cadastral-id", "RODZAJ": "k"},
    }


def test_cadastral_id_is_preferred_and_missing_attributes_stay_null() -> None:
    normalized = normalize_feature(sample_feature())

    assert normalized[0] == "stable-cadastral-id"
    assert normalized[3] == "k"
    assert normalized[4] is None
    assert normalized[2].obj == sample_feature()["properties"]


def test_non_polygon_buildings_are_rejected() -> None:
    feature = sample_feature()
    feature["geometry"] = {"type": "Point", "coordinates": [21.0, 52.2]}

    with pytest.raises(ValueError, match="Polygon"):
        normalize_feature(feature)


def test_truncated_wfs_snapshots_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "partial.geojson"
    path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": [sample_feature()], "numberMatched": 20}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Incomplete snapshot"):
        list(read_features(path))


def test_empty_snapshot_cannot_clear_the_dataset(tmp_path: Path) -> None:
    path = tmp_path / "empty.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        list(read_features(path))


def test_complete_snapshot_preserves_geometry(tmp_path: Path) -> None:
    feature = sample_feature()
    path = tmp_path / "complete.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": [feature], "numberMatched": 1}),
        encoding="utf-8",
    )

    assert list(read_features(path)) == [feature]
