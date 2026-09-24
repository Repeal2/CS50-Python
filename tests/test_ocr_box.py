"""The OCR box's geometry: where its parts go, what a click grabs, and what dragging does (see
screen.ocr_box). The Tk window itself needs a display, so it isn't exercised here."""

from meeting_scribe.screen.ocr_box import (
    BAR_HEIGHT,
    HANDLE_NAMES,
    MARGIN,
    MIN_AREA_HEIGHT,
    MIN_AREA_WIDTH,
    BoxLayout,
    default_area,
    drag_area,
    hit_test,
    keep_on_screen,
    overlaps,
)

AREA = {"left": 100, "top": 200, "width": 400, "height": 120}
SCREEN = {"left": -1920, "top": 0, "width": 3840, "height": 1080}  # a second monitor left of the main one


def _centre(rect):
    x0, y0, x1, y1 = rect
    return (x0 + x1) // 2, (y0 + y1) // 2


def test_the_window_surrounds_the_area_with_the_button_bar_above_its_top_left():
    layout = BoxLayout(AREA)
    assert layout.window == {
        "left": 100 - MARGIN, "top": 200 - MARGIN - BAR_HEIGHT,
        "width": 400 + 2 * MARGIN, "height": 120 + 2 * MARGIN + BAR_HEIGHT,
    }
    x0, y0, x1, y1 = layout.inner
    # The area inside the window lands exactly on the screen area being read.
    assert (layout.window["left"] + x0, layout.window["top"] + y0) == (100, 200)
    assert (x1 - x0, y1 - y0) == (400, 120)
    assert layout.bar[:2] == (0, 0) and layout.bar[3] <= y0 - MARGIN


def test_nothing_the_box_draws_overlaps_the_area_being_read():
    layout = BoxLayout(AREA)
    ix0, iy0, ix1, iy1 = layout.inner
    for x0, y0, x1, y1 in [*layout.handles().values(), layout.bar, layout.toggle_button, layout.grip]:
        assert x1 <= ix0 or x0 >= ix1 or y1 <= iy0 or y0 >= iy1


def test_a_click_on_each_part_grabs_that_part():
    layout = BoxLayout(AREA)
    assert hit_test(layout, *_centre(layout.toggle_button)) == "toggle"
    assert hit_test(layout, *_centre(layout.grip)) == "move"
    for name, rect in layout.handles().items():
        assert hit_test(layout, *_centre(rect)) == name
    ix0, iy0, ix1, iy1 = layout.inner
    assert hit_test(layout, ix0 + 100, iy0 - 3) == "move"  # the frame band, between handles
    assert hit_test(layout, *_centre(layout.inner)) is None  # the area itself: click-through
    assert hit_test(layout, layout.window["width"] - 2, 5) is None  # beside the bar


def test_the_button_bar_is_reachable_on_a_narrow_box():
    layout = BoxLayout({"left": 0, "top": 100, "width": MIN_AREA_WIDTH, "height": MIN_AREA_HEIGHT})
    assert layout.window["width"] >= layout.bar[2]
    assert hit_test(layout, *_centre(layout.toggle_button)) == "toggle"


def test_moving_shifts_the_area_without_resizing_it():
    assert drag_area(AREA, "move", 30, -15) == {"left": 130, "top": 185, "width": 400, "height": 120}


def test_each_handle_moves_only_its_own_edges():
    assert drag_area(AREA, "se", 50, 20) == {"left": 100, "top": 200, "width": 450, "height": 140}
    assert drag_area(AREA, "nw", 50, 20) == {"left": 150, "top": 220, "width": 350, "height": 100}
    assert drag_area(AREA, "e", 50, 20) == {"left": 100, "top": 200, "width": 450, "height": 120}
    assert drag_area(AREA, "n", 50, 20) == {"left": 100, "top": 220, "width": 400, "height": 100}
    assert set(HANDLE_NAMES) == {"nw", "n", "ne", "e", "se", "s", "sw", "w"}


def test_resizing_stops_at_the_minimum_size_holding_the_opposite_edge():
    shrunk = drag_area(AREA, "nw", 1000, 1000)
    assert (shrunk["width"], shrunk["height"]) == (MIN_AREA_WIDTH, MIN_AREA_HEIGHT)
    assert shrunk["left"] + shrunk["width"] == 500 and shrunk["top"] + shrunk["height"] == 320
    shrunk = drag_area(AREA, "se", -1000, -1000)
    assert (shrunk["left"], shrunk["top"], shrunk["width"], shrunk["height"]) == (
        100, 200, MIN_AREA_WIDTH, MIN_AREA_HEIGHT
    )


def test_the_box_cannot_be_dragged_off_screen_including_onto_a_monitor_to_the_left():
    moved = drag_area(AREA, "move", -5000, -5000, SCREEN)
    assert moved["left"] == SCREEN["left"] + MARGIN
    assert moved["top"] == SCREEN["top"] + MARGIN + BAR_HEIGHT  # the button bar stays on screen
    assert drag_area(AREA, "move", -2000, 0, SCREEN)["left"] == -1900  # fine: it's on the left monitor


def test_a_box_bigger_than_the_screen_is_shrunk_to_fit():
    fitted = keep_on_screen({"left": 0, "top": 0, "width": 9000, "height": 9000}, SCREEN)
    assert fitted["left"] >= SCREEN["left"] and fitted["left"] + fitted["width"] <= SCREEN["left"] + SCREEN["width"]


def test_a_new_box_starts_over_the_bottom_of_the_call_window():
    work_area = {"left": 0, "top": 0, "width": 1920, "height": 1040}
    window = {"left": 200, "top": 100, "width": 1000, "height": 800}
    area = default_area(work_area, window)
    assert window["left"] < area["left"] and area["left"] + area["width"] < window["left"] + window["width"]
    assert area["top"] > window["top"] + window["height"] // 2
    assert area["top"] + area["height"] <= window["top"] + window["height"]


def test_without_a_call_window_a_new_box_starts_in_the_middle_of_the_screen():
    area = default_area({"left": 0, "top": 0, "width": 1920, "height": 1040})
    assert abs((area["left"] + area["width"] / 2) - 960) <= 1
    assert abs((area["top"] + area["height"] / 2) - 520) <= 1


def test_a_box_left_on_an_unplugged_monitor_is_noticed():
    assert overlaps(AREA, SCREEN)
    assert not overlaps({"left": 5000, "top": 0, "width": 100, "height": 100}, SCREEN)


def test_the_button_bar_of_a_narrow_box_stays_on_screen_at_the_right_edge():
    from meeting_scribe.screen.ocr_box import BAR_WIDTH

    narrow = {"left": 1900, "top": 500, "width": MIN_AREA_WIDTH, "height": 50}
    placed = keep_on_screen(narrow, SCREEN)
    assert BoxLayout(placed).window["left"] + BAR_WIDTH <= SCREEN["left"] + SCREEN["width"]
