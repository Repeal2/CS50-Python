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


def test_window_region_target_label_includes_window_title_and_picked_size():
    target = WindowRegionTarget(
        hwnd=42,
        window_title="Microsoft Teams",
        offset_left_frac=0.1,
        offset_top_frac=0.2,
        width_frac=0.3,
        height_frac=0.1,
        picked_width=300,
        picked_height=80,
    )
    assert target.label == "Microsoft Teams — custom area (300x80)"


def test_window_region_target_mss_region_tracks_the_windows_current_position():
    # offset/size fractions computed as if picked at left+90,top+140,width=300,height=80 against a
    # 900x700 window (i.e. offset_left_frac=100/1000... see the derivation in the pinning test below;
    # kept as simple round fractions here so the expected pixels are easy to verify by hand).
    target = WindowRegionTarget(
        hwnd=42,
        window_title="Microsoft Teams",
        offset_left_frac=0.1,
        offset_top_frac=0.2,
        width_frac=1 / 3,
        height_frac=0.1,
        picked_width=300,
        picked_height=80,
    )

    # Same fractional offset/size, different window position (as if the window moved to another
    # monitor, unresized) -> the resolved region moves by exactly the same amount the window did.
    assert target.mss_region({"left": 100, "top": 200, "width": 900, "height": 700}) == {
        "left": 190,
        "top": 340,
        "width": 300,
        "height": 70,
    }
    assert target.mss_region({"left": -1920, "top": 50, "width": 900, "height": 700}) == {
        "left": -1830,
        "top": 190,
        "width": 300,
        "height": 70,
    }


def test_window_region_target_mss_region_scales_with_the_windows_current_size():
    """A window resized to double its picked width/height should double the pinned area's offset and
    size too, so it stays in the same relative spot rather than drifting toward a corner."""
    target = WindowRegionTarget(
        hwnd=42,
        window_title="Microsoft Teams",
        offset_left_frac=0.25,
        offset_top_frac=0.5,
        width_frac=0.5,
        height_frac=0.25,
        picked_width=225,
        picked_height=175,
    )

    assert target.mss_region({"left": 0, "top": 0, "width": 900, "height": 700}) == {
        "left": 225,
        "top": 350,
        "width": 450,
        "height": 175,
    }
    assert target.mss_region({"left": 0, "top": 0, "width": 1800, "height": 1400}) == {
        "left": 450,
        "top": 700,
        "width": 900,
        "height": 350,
    }


def test_pin_region_to_window_computes_fractional_offset_from_the_windows_current_bounds():
    region = RegionTarget(left=190, top=340, width=300, height=70)
    window = WindowTarget(hwnd=42, title="Microsoft Teams")
    window_rect = {"left": 100, "top": 200, "width": 900, "height": 700}

    pinned = pin_region_to_window(region, window, window_rect)

    assert pinned == WindowRegionTarget(
        hwnd=42,
        window_title="Microsoft Teams",
        offset_left_frac=0.1,
        offset_top_frac=0.2,
        width_frac=1 / 3,
        height_frac=0.1,
        picked_width=300,
        picked_height=70,
    )
    # Resolving against the same window_rect it was pinned from reproduces the original absolute region.
    assert pinned.mss_region(window_rect) == region.mss_region
