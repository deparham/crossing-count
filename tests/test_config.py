import json
from pathlib import Path
from typing import Any

import pytest

from crossing_count.config import ConfigError, bind, load_config, parse_config

ROOT = Path(__file__).resolve().parents[1]


def minimal(**extra: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "site": "S",
        "sensor": "CAM",
        "line": [[0.2, 0.4], [0.8, 0.4]],
        "inside_side": "below",
        "fps_assumed": 10,
    }
    raw.update(extra)
    return raw


def test_line_only_config_is_valid_and_counts_every_crossing() -> None:
    cfg = parse_config(minimal())
    assert cfg.mask_zone is None and cfg.filter_zones == ()
    assert cfg.rule == "line"
    bind(cfg, 640, 480)


def test_rule_reflects_optional_zones() -> None:
    mask = [[0.2, 0.5], [0.8, 0.5], [0.8, 0.6], [0.2, 0.6]]
    filt = [[[0.1, 0.42], [0.9, 0.42], [0.9, 0.9], [0.1, 0.9]]]
    assert parse_config(minimal(mask_zone=mask)).rule == "line+mask"
    assert parse_config(minimal(filter_zones=filt)).rule == "line+filter"
    assert parse_config(minimal(mask_zone=mask, filter_zones=filt)).rule == "line+mask+filter"


def test_brief_example_config_loads() -> None:
    cfg = load_config(ROOT / "sites" / "example_carindale.json")
    assert cfg.rule == "line+mask"
    assert len(cfg.sha256) == 64
    bind(cfg, 1280, 960)


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config(minimal(min_dwell_zone_s=0.4))
    with pytest.raises(ConfigError, match="unknown gate"):
        parse_config(minimal(gate={"line_margn": 0.2}))


def test_out_of_range_coordinates_are_rejected() -> None:
    with pytest.raises(ConfigError, match="0-1"):
        parse_config(minimal(line=[[0.2, 0.4], [1.2, 0.4]]))


def test_mask_zone_on_the_outside_is_rejected() -> None:
    cfg = parse_config(minimal(mask_zone=[[0.2, 0.1], [0.8, 0.1], [0.8, 0.2], [0.2, 0.2]]))
    with pytest.raises(ConfigError, match="inside"):
        bind(cfg, 640, 480)


def test_mask_zone_straddling_the_line_warns() -> None:
    cfg = parse_config(minimal(mask_zone=[[0.3, 0.35], [0.7, 0.35], [0.7, 0.6], [0.3, 0.6]]))
    assert any("overlaps" in w for w in bind(cfg, 640, 480).warnings)


def test_thin_mask_band_with_a_dwell_warns_and_zero_dwell_is_allowed() -> None:
    band = [[0.2, 0.5], [0.8, 0.5], [0.8, 0.52], [0.2, 0.52]]  # ~10 px deep at 480
    cfg = parse_config(minimal(mask_zone=band, min_dwell_in_zone_s=0.4))
    assert any("thin mask band" in w for w in bind(cfg, 640, 480).warnings)
    cfg0 = parse_config(minimal(mask_zone=band, min_dwell_in_zone_s=0))
    assert cfg0.min_dwell_in_zone_s == 0.0
    assert not any("thin" in w for w in bind(cfg0, 640, 480).warnings)
    with pytest.raises(ConfigError):
        parse_config(minimal(pending_timeout_s=0))


def test_bow_tie_zone_is_rejected() -> None:
    # Corners clicked top-left, bottom-left, top-right, bottom-right: the edges cross.
    bow = [[0.2, 0.5], [0.2, 0.55], [0.8, 0.5], [0.8, 0.55]]
    with pytest.raises(ConfigError, match="edges cross"):
        bind(parse_config(minimal(mask_zone=bow)), 640, 480)
    with pytest.raises(ConfigError, match="edges cross"):
        bind(parse_config(minimal(filter_zones=[bow])), 640, 480)


def test_aspect_mismatch_is_rejected() -> None:
    cfg = parse_config(minimal(traced_on={"tile_size": [640, 480]}))
    bind(cfg, 1280, 960)
    with pytest.raises(ConfigError, match="different shape"):
        bind(cfg, 640, 360)


def test_bind_applies_origin() -> None:
    g = bind(parse_config(minimal()), 640, 480, origin=(640, 120))
    assert g.line[0].tolist() == [640 + 0.2 * 640, 120 + 0.4 * 480]


def test_gate_overrides_round_trip(tmp_path: Path) -> None:
    raw = minimal(gate={"line_margin": 0.2}, filter_zones=[], exclusion_zones=[])
    cfg = parse_config(raw)
    assert cfg.gate.line_margin == 0.2
    p = tmp_path / "c.json"
    p.write_text(json.dumps(cfg.to_json_dict()))
    again = load_config(p)
    assert again.to_json_dict() == cfg.to_json_dict()
