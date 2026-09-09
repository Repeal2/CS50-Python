from meeting_scribe.screen.region_picker import (
    RegionTarget,
    WindowRegionTarget,
    _frame_geometries,
    _region_from_drag,
    pin_region_to_window,
)
from meeting_scribe.screen.window_picker import WindowTarget


def test_region_from_drag_normalizes_any_drag_direction():
    # Dragged bottom-right to top-left — still yields the same normalized rectangle.
    assert _region_from_drag(200, 150, 50, 50) == RegionTarget(left=50, top=50, width=150, height=100)


def test_region_from_drag_rejects_too_small_a_selection():
    assert _region_from_drag(100, 100, 102, 102) is None


def test_region_from_drag_accepts_the_minimum_size():
    result = _region_from_drag(0, 0, 8, 8)
    assert result == RegionTarget(left=0, top=0, width=8, height=8)


def test_region_target_label_includes_size_and_position():
    target = RegionTarget(left=10, top=20, width=300, height=200)
    assert target.label == "Custom area (300x200 at 10,20)"


def test_region_target_mss_region_matches_mss_dict_shape():
    target = RegionTarget(left=10, top=20, width=300, height=200)
    assert target.mss_region == {"left": 10, "top": 20, "width": 300, "height": 200}


def test_frame_geometries_form_a_hollow_frame_outside_the_target():
    target = RegionTarget(left=100, top=100, width=200, height=150)
    top, bottom, left, right = _frame_geometries(target.mss_region, thickness=3)

    # None of the four strips overlap the target's own interior — they sit just outside its bounds.
    assert top == "206x3+97+97"
    assert bottom == "206x3+97+250"
    assert left == "3x150+97+100"
    assert right == "3x150+300+100"


def test_window_region_target_label_includes_window_title_and_size():
    target = WindowRegionTarget(
        hwnd=42, window_title="Microsoft Teams", offset_left=10, offset_top=20, width=300, height=80
    )
    assert target.label == "Microsoft Teams — custom area (300x80)"


def test_window_region_target_mss_region_tracks_the_windows_current_position():
    target = WindowRegionTarget(
        hwnd=42, window_title="Microsoft Teams", offset_left=10, offset_top=20, width=300, height=80
    )

    # Same offset, different window position (as if the window moved to another monitor) -> the
    # resolved region moves by exactly the same amount the window did.
    assert target.mss_region({"left": 100, "top": 200, "width": 900, "height": 700}) == {
        "left": 110,
        "top": 220,
        "width": 300,
        "height": 80,
    }
    assert target.mss_region({"left": -1920, "top": 50, "width": 900, "height": 700}) == {
        "left": -1910,
        "top": 70,
        "width": 300,
        "height": 80,
    }


def test_pin_region_to_window_computes_offset_from_the_windows_current_bounds():
    region = RegionTarget(left=150, top=220, width=300, height=80)
    window = WindowTarget(hwnd=42, title="Microsoft Teams")
    window_rect = {"left": 100, "top": 200, "width": 900, "height": 700}

    pinned = pin_region_to_window(region, window, window_rect)

    assert pinned == WindowRegionTarget(
        hwnd=42, window_title="Microsoft Teams", offset_left=50, offset_top=20, width=300, height=80
    )
    # Resolving against the same window_rect it was pinned from reproduces the original absolute region.
    assert pinned.mss_region(window_rect) == region.mss_region
