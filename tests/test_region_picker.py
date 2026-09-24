from meeting_scribe.screen.region_picker import RegionTarget, _region_from_drag


def test_region_from_drag_normalizes_any_drag_direction():
    # Dragged bottom-right to top-left — still yields the same normalized rectangle.
    assert _region_from_drag(200, 150, 50, 50) == RegionTarget(left=50, top=50, width=150, height=100)


def test_region_from_drag_rejects_too_small_a_selection():
    assert _region_from_drag(100, 100, 102, 102) is None


def test_region_from_drag_accepts_the_minimum_size():
    result = _region_from_drag(0, 0, 8, 8)
    assert result == RegionTarget(left=0, top=0, width=8, height=8)


def test_region_target_mss_region_matches_mss_dict_shape():
    target = RegionTarget(left=10, top=20, width=300, height=200)
    assert target.mss_region == {"left": 10, "top": 20, "width": 300, "height": 200}
