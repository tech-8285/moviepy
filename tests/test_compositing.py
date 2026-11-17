"""Compositing tests for use with pytest."""

import os

import numpy as np

import pytest

from moviepy import *


class ClipPixelTest:
    ALLOWABLE_COLOR_VARIATION = 3  # from 0-767: how much mismatch do we allow

    def __init__(self, clip):
        self.clip = clip

    def expect_color_at(self, ts, expected, xy=[0, 0]):
        frame = self.clip.frame_function(ts)
        r, g, b = expected
        actual = frame[xy[1]][xy[0]]
        diff = abs(actual[0] - r) + abs(actual[1] - g) + abs(actual[2] - b)

        mismatch = diff > ClipPixelTest.ALLOWABLE_COLOR_VARIATION
        assert (
            not mismatch
        ), "Expected (%02x,%02x,%02x) but got (%02x,%02x,%02x) at timestamp %s" % (
            *expected,
            *actual,
            ts,
        )


# Small helpers for pixel-level compositing assertions. ``xy`` is given as
# (x, y) so that it reads like a position, while numpy arrays are indexed by
# (row, column).
def assert_pixel_color(frame, xy, expected_color, atol=1):
    x, y = xy
    actual = frame[y, x]
    assert np.allclose(
        actual, expected_color, atol=atol
    ), f"Expected color {expected_color} at {xy} but got {actual}"


def assert_pixel_alpha(mask, xy, expected_alpha, atol=1e-5):
    x, y = xy
    actual = mask[y, x]
    assert abs(actual - expected_alpha) <= atol, (
        f"Expected alpha {expected_alpha} at {xy} but got {actual}"
    )


def assert_only_region_has_alpha(mask, region):
    """Asserts that the mask is non-zero exactly inside ``region``.

    ``region`` is a tuple ``(x1, x2, y1, y2)`` of half-open bounds.
    """
    x1, x2, y1, y2 = region
    expected_nonzero = np.zeros(mask.shape, dtype=bool)
    expected_nonzero[y1:y2, x1:x2] = True
    assert np.array_equal(mask > 0, expected_nonzero)


def test_clips_array(util):
    red = ColorClip((1024, 800), color=(255, 0, 0))
    green = ColorClip((1024, 800), color=(0, 255, 0))
    blue = ColorClip((1024, 800), color=(0, 0, 255))

    video = clips_array([[red, green, blue]])

    with pytest.raises(ValueError):  # duration not set
        video.with_effects([vfx.Resize(width=480)]).write_videofile(
            os.path.join(util.TMP_DIR, "test_clips_array.mp4")
        )


def test_clips_array_duration(util):
    filename = os.path.join(util.TMP_DIR, "test_clips_array.mp4")

    # NOTE: anyone knows what behaviour this sets ? If yes please replace
    # this comment.
    red = ColorClip((256, 200), color=(255, 0, 0))
    green = ColorClip((256, 200), color=(0, 255, 0))
    blue = ColorClip((256, 200), color=(0, 0, 255))

    video = clips_array([[red, green, blue]]).with_duration(5)
    with pytest.raises(AttributeError):  # fps not set
        video.write_videofile(filename)

    # this one should work correctly
    red.fps = green.fps = blue.fps = 30
    video = clips_array([[red, green, blue]]).with_duration(5)
    video.write_videofile(filename)


def test_concatenate_self(util):
    clip = BitmapClip([["AAA", "BBB"], ["CCC", "DDD"]], fps=1)
    target = BitmapClip([["AAA", "BBB"], ["CCC", "DDD"]], fps=1)

    concatenated = concatenate_videoclips([clip])

    concatenated.write_videofile(
        os.path.join(util.TMP_DIR, "test_concatenate_self.mp4")
    )
    assert concatenated == target


def test_concatenate_floating_point(util):
    """
    >>> print("{0:.20f}".format(1.12))
    1.12000000000000010658

    This test uses duration=1.12 to check that it still works when the clip
    duration is represented as being bigger than it actually is. Fixed in #1195.
    """
    clip = ColorClip([100, 50], color=[255, 128, 64], duration=1.12).with_fps(25.0)
    concat = concatenate_videoclips([clip])
    concat.write_videofile(os.path.join(util.TMP_DIR, "concat.mp4"), preset="ultrafast")


def test_blit_with_opacity():
    # has one second R, one second G, one second B
    size = (2, 2)
    clip1 = (
        ColorClip(size, color=(255, 0, 0), duration=1)
        + ColorClip(size, color=(0, 255, 0), duration=1)
        + ColorClip(size, color=(0, 0, 255), duration=1)
    )

    # overlay green at half opacity during first 2 sec
    clip2 = ColorClip(size, color=(0, 255, 0), duration=2).with_opacity(0.5)
    composite = CompositeVideoClip([clip1, clip2])
    bt = ClipPixelTest(composite)

    # red + 50% green
    bt.expect_color_at(0.5, (0x7F, 0x7F, 0x00))
    # green + 50% green
    bt.expect_color_at(1.5, (0x00, 0xFF, 0x00))
    # blue is after 2s, so keep untouched
    bt.expect_color_at(2.5, (0x00, 0x00, 0xFF))


def test_compositing_masks(util):
    # Has one R 30%, one G 30%, one B 30%
    clip1 = ColorClip((100, 100), (255, 0, 0, 76.5)).with_duration(2)
    clip2 = ColorClip((50, 50), (0, 255, 0, 76.5)).with_duration(2)
    clip3 = ColorClip((25, 25), (0, 0, 255, 76.5)).with_duration(2)

    compostite_clip1 = CompositeVideoClip(
        [clip1, clip2.with_position(("center", "center"))],
        bg_color=None,
    )
    compostite_clip2 = CompositeVideoClip(
        [compostite_clip1, clip3.with_position(("center", "center"))],
        bg_color=None,
    )

    # Load output file and check transparency
    frame = compostite_clip2.mask.get_frame(1)

    # We check opacity with one, two and three layers
    # Allow for a bit of tolerance (about 1%) to account
    # for rounding errors
    opacity1 = frame[50, 10]
    opacity2 = frame[50, 30]
    opacity3 = frame[50, 50]
    assert abs(opacity1 - 0.3) < 0.01
    assert abs(opacity2 - 0.51) < 0.01
    assert abs(opacity3 - 0.657) < 0.01


def test_compositing_with_transparency_colors(util):
    # Has one R 30%, one G 30%, one B 30%
    clip1 = ColorClip((100, 100), (255, 0, 0, 76.5)).with_duration(2)
    clip2 = ColorClip((50, 50), (0, 255, 0, 76.5)).with_duration(2)
    clip3 = ColorClip((25, 25), (0, 0, 255, 76.5)).with_duration(2)

    compostite_clip1 = CompositeVideoClip(
        [clip1, clip2.with_position(("center", "center"))],
        bg_color=None,  # No background color, so it should be transparent
    )
    compostite_clip2 = CompositeVideoClip(
        [compostite_clip1, clip3.with_position(("center", "center"))],
        bg_color=None,
    )

    # Load output file and check transparency
    frame = compostite_clip2.get_frame(1)
    mask = compostite_clip2.mask.get_frame(1)

    # We check color with 1 layer
    # We add a bit of tolerance (about 1%) to account
    # For possible rounding errors
    color1 = frame[50, 10]
    opacity1 = mask[50, 10]
    assert np.allclose(color1, [255, 0, 0], atol=1)
    assert abs(opacity1 - 0.3) < 0.01

    # With 2 layers
    color2 = frame[50, 30]
    opacity2 = mask[50, 30]
    assert np.allclose(color2, [105, 150, 0], atol=1)
    assert abs(opacity2 - 0.51) < 0.01

    # With 3 layers
    color3 = frame[50, 50]
    opacity3 = mask[50, 50]
    assert np.allclose(color3, [56, 81, 116], atol=1)
    assert abs(opacity3 - 0.657) < 0.01


def test_slide_in():
    duration = 0.1
    size = (10, 1)
    fps = 10
    color = (255, 0, 0)

    # left and right sides
    clip = ColorClip(
        color=color,
        duration=duration,
        size=size,
    ).with_fps(fps)

    for side in ["left", "right"]:
        new_clip = CompositeVideoClip(
            [clip.with_effects([vfx.SlideIn(duration, side)])]
        )

        for t in np.arange(0, duration, duration / fps):
            n_reds, n_reds_expected = (0, int(t * 100))

            if t:
                assert n_reds_expected

            if n_reds_expected == 7:  # skip 7 due to inaccurate frame
                continue

            for r, g, b in new_clip.get_frame(t)[0]:
                if r == color[0] and g == color[1] and g == color[2]:
                    n_reds += 1

            assert n_reds == n_reds_expected

    # top and bottom sides
    clip = ColorClip(
        color=color,
        duration=duration,
        size=(size[1], size[0]),
    ).with_fps(fps)

    for side in ["top", "bottom"]:
        new_clip = CompositeVideoClip(
            [clip.with_effects([vfx.SlideIn(duration, side)])]
        )
        for t in np.arange(0, duration, duration / fps):
            n_reds, n_reds_expected = (0, int(t * 100))

            if t:
                assert n_reds_expected

            if n_reds_expected == 7:  # skip 7 due to inaccurate frame
                continue

            for row in new_clip.get_frame(t):
                r, g, b = row[0]

                if r == color[0] and g == color[1] and g == color[2]:
                    n_reds += 1

            assert n_reds == n_reds_expected


def test_slide_out():
    duration = 0.1
    size = (11, 1)
    fps = 10
    color = (255, 0, 0)

    # left and right sides
    clip = ColorClip(
        color=color,
        duration=duration,
        size=size,
    ).with_fps(fps)

    for side in ["left", "right"]:
        new_clip = CompositeVideoClip(
            [clip.with_effects([vfx.SlideOut(duration, side)])]
        )

        for t in np.arange(0, duration, duration / fps):
            n_reds, n_reds_expected = (0, round(11 - t * 100, 6))

            if t:
                assert n_reds_expected

            for r, g, b in new_clip.get_frame(t)[0]:
                if r == color[0] and g == color[1] and g == color[2]:
                    n_reds += 1

            assert n_reds == n_reds_expected

    # top and bottom sides
    clip = ColorClip(
        color=color,
        duration=duration,
        size=(size[1], size[0]),
    ).with_fps(fps)

    for side in ["top", "bottom"]:
        new_clip = CompositeVideoClip(
            [clip.with_effects([vfx.SlideOut(duration, side)])]
        )
        for t in np.arange(0, duration, duration / fps):
            n_reds, n_reds_expected = (0, round(11 - t * 100, 6))

            if t:
                assert n_reds_expected

            for row in new_clip.get_frame(t):
                r, g, b = row[0]

                if r == color[0] and g == color[1] and g == color[2]:
                    n_reds += 1

            assert n_reds == n_reds_expected


def test_compositing_transparent_background_layers():
    """Three semi-transparent layers over a transparent background.

    Checks that both the resulting color and the resulting alpha follow the
    source layer stack: 0.5 for a single layer, 0.75 for two stacked layers
    and 0.875 for three, while the colors are the "passing light" blend of
    the layers underneath.
    """
    size = (8, 6)
    red = ColorClip((8, 3), (255, 0, 0), duration=2).with_opacity(0.5)
    green = (
        ColorClip((6, 6), (0, 255, 0), duration=2)
        .with_opacity(0.5)
        .with_position((-2, 2))
    )
    blue = (
        ColorClip((4, 4), (0, 0, 255), duration=2)
        .with_opacity(0.5)
        .with_position((2, 2))
    )

    composite = CompositeVideoClip([red, green, blue], size=size, bg_color=None)
    frame = composite.get_frame(0.5)
    mask = composite.mask.get_frame(0.5)

    assert frame.dtype == np.uint8
    assert frame.shape == (6, 8, 3)
    assert mask.shape == (6, 8)

    # Red only, alpha comes from a single 0.5 layer.
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 0), 0.5)

    # Red + green (green overflows the bottom-left corner, so x=0 is green
    # itself and only the overlap area blends).
    assert_pixel_color(frame, (0, 2), (85, 170, 0))
    assert_pixel_alpha(mask, (0, 2), 0.75)
    assert_pixel_color(frame, (0, 3), (0, 255, 0))
    assert_pixel_alpha(mask, (0, 3), 0.5)

    # Red + green + blue in the middle.
    assert_pixel_color(frame, (2, 2), (36, 73, 146))
    assert_pixel_alpha(mask, (2, 2), 0.875)

    # Green + blue (blue is on top, so the blend is computed on the blue).
    assert_pixel_color(frame, (2, 3), (0, 85, 170))
    assert_pixel_alpha(mask, (2, 3), 0.75)

    # Only blue.
    assert_pixel_color(frame, (5, 3), (0, 0, 255))
    assert_pixel_alpha(mask, (5, 3), 0.5)

    # Uncovered canvas area stays fully transparent.
    assert_pixel_alpha(mask, (7, 5), 0.0)
    assert_pixel_alpha(mask, (6, 4), 0.0)
    assert_pixel_color(frame, (7, 5), (0, 0, 0))

    # Coverage map: the red band covers the first three rows completely,
    # while green and blue cover x=0..5 of the three bottom rows, leaving
    # x=6..7 of those rows empty.
    expected_coverage = np.zeros((6, 8), dtype=bool)
    expected_coverage[0:3, :] = True
    expected_coverage[3:6, 0:6] = True
    assert np.array_equal(mask > 0, expected_coverage)


def test_compositing_multi_layer_masks():
    """Stacked gradient masks keep their own alpha and blend over each other.

    The lower layer is a 50% red sheet, the upper one a blue clip carrying a
    0.2..1.0 alpha gradient. A third clip whose mask is fully zero must not
    paint anything, and must not brighten the layers below.
    """
    size = (6, 4)
    bottom = ColorClip((6, 4), (255, 0, 0), duration=2).with_opacity(0.5)

    gradient = np.array(
        [[0.2, 0.4, 0.6], [0.4, 0.6, 0.8], [0.6, 0.8, 1.0]], dtype=float
    )
    top = (
        ColorClip((3, 3), (0, 0, 255), duration=2)
        .with_mask(ImageClip(gradient, is_mask=True, duration=2))
        .with_position((2, 1))
    )

    # A clip with an all-zero mask is fully transparent.
    transparent = (
        ColorClip((2, 2), (0, 255, 0), duration=2)
        .with_mask(ImageClip(np.zeros((2, 2)), is_mask=True, duration=2))
        .with_position((0, 0))
    )

    composite = CompositeVideoClip(
        [bottom, top, transparent], size=size, bg_color=None
    )
    frame = composite.get_frame(0.0)
    mask = composite.mask.get_frame(0.0)

    # Fully transparent clip leaves the background untouched, both in color
    # and in alpha.
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 0), 0.5)
    assert_pixel_color(frame, (1, 1), (255, 0, 0))
    assert_pixel_alpha(mask, (1, 1), 0.5)

    # Bottom layer outside the top clip.
    assert_pixel_color(frame, (5, 3), (255, 0, 0))
    assert_pixel_alpha(mask, (5, 3), 0.5)
    assert_pixel_color(frame, (2, 0), (255, 0, 0))

    # top mask == 0.2 at its own (0, 0) located at canvas (2, 1).
    assert_pixel_alpha(mask, (2, 1), 0.6)
    assert_pixel_color(frame, (2, 1), (170, 0, 85))

    # top mask == 0.6 at its own (1, 1) located at canvas (3, 2).
    assert_pixel_alpha(mask, (3, 2), 0.8)
    assert_pixel_color(frame, (3, 2), (64, 0, 191))

    # top mask == 1.0 at its own (2, 2) located at canvas (4, 3).
    assert_pixel_alpha(mask, (4, 3), 1.0)
    assert_pixel_color(frame, (4, 3), (0, 0, 255))

    # Alpha keeps growing with the gradient without ever exceeding 1.
    assert np.all(mask >= 0.5 - 1e-7)
    assert np.all(mask <= 1.0 + 1e-7)


def test_compositing_dynamic_position_carries_mask():
    """A moving clip must carry its mask along on every frame.

    The mask is a 2x2 gradient, so an offset by one pixel or a mask sampled
    at the wrong time would produce different numbers.
    """
    alpha = np.array([[0.2, 0.4], [0.6, 0.8]])
    mask = ImageClip(alpha, is_mask=True, duration=5)
    moving = (
        ColorClip((2, 2), (0, 255, 0), duration=5)
        .with_mask(mask)
        .with_position(lambda t: (1 + 3 * int(t), 1))
    )

    composite = CompositeVideoClip([moving], size=(13, 4), bg_color=None)

    for t in range(4):
        x = 1 + 3 * t
        frame = composite.get_frame(float(t))
        frame_mask = composite.mask.get_frame(float(t))

        assert np.allclose(frame_mask[1:3, x : x + 2], alpha)
        assert_pixel_color(frame, (x, 1), (0, 255, 0))
        assert_pixel_color(frame, (x + 1, 2), (0, 255, 0))

        # The strip the clip used to occupy on the previous frame is empty.
        if t > 0:
            previous_x = 1 + 3 * (t - 1)
            assert np.all(frame_mask[1:3, previous_x : previous_x + 2] == 0)
            assert_pixel_color(frame, (previous_x, 1), (0, 0, 0))


def test_compositing_dynamic_position_partial_exit():
    """A clip sliding out of the canvas only contributes its visible part.

    The asymmetric mask proves that the correct source slice is used: when
    the left half has left the canvas, the remaining columns must show the
    right half of the mask, not the left one.
    """
    alpha = np.array([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])
    mask = ImageClip(alpha, is_mask=True, duration=4)
    moving = (
        ColorClip((4, 2), (0, 0, 255), duration=4)
        .with_mask(mask)
        .with_position(lambda t: (3 * int(t) - 2, 2))
    )

    composite = CompositeVideoClip([moving], size=(10, 6), bg_color=None)

    # t=0: clip at x=-2, so only source columns 2 and 3 are visible.
    frame_mask = composite.mask.get_frame(0.0)
    assert np.allclose(frame_mask[2:4, 0:2], alpha[:, 2:4])
    assert np.all(frame_mask[2:4, 2:] == 0)

    # t=2: clip at x=4, fully visible.
    frame_mask = composite.mask.get_frame(2.0)
    assert np.allclose(frame_mask[2:4, 4:8], alpha)
    assert np.all(frame_mask[2:4, 0:4] == 0)
    assert np.all(frame_mask[2:4, 8:] == 0)

    # t=3: clip at x=7, only source columns 0..2 are visible.
    frame_mask = composite.mask.get_frame(3.0)
    assert np.allclose(frame_mask[2:4, 7:10], alpha[:, 0:3])
    assert np.all(frame_mask[2:4, 0:7] == 0)

    # The fully visible frame keeps the clip's body color everywhere.
    frame = composite.get_frame(2.0)
    assert np.all(frame[2:4, 4:8] == np.array([0, 0, 255], dtype=np.uint8))


def test_compositing_same_layer_follows_list_order():
    """Clips sharing a ``layer_index`` are stacked in list order.

    The last clip in the list wins, and reversing the list reverses which
    color reaches the top, while the combined alpha is unaffected.
    """
    size = (6, 6)

    def make_clips():
        red = (
            ColorClip((4, 4), (255, 0, 0), duration=2)
            .with_opacity(0.5)
            .with_position((0, 0))
        )
        blue = (
            ColorClip((4, 4), (0, 0, 255), duration=2)
            .with_opacity(0.5)
            .with_position((2, 2))
        )
        return red, blue

    red, blue = make_clips()
    red_first = CompositeVideoClip([red, blue], size=size, bg_color=None)

    red, blue = make_clips()
    blue_first = CompositeVideoClip([blue, red], size=size, bg_color=None)

    # Both clips keep the default layer index, so only the order matters.
    assert [clip.layer_index for clip in red_first.clips] == [0, 0]
    assert [clip.layer_index for clip in blue_first.clips] == [0, 0]

    frame = red_first.get_frame(0.0)
    mask = red_first.mask.get_frame(0.0)
    # Blue is last, so blue is on top of red in the overlap.
    assert_pixel_color(frame, (3, 3), (85, 0, 170))
    assert_pixel_alpha(mask, (3, 3), 0.75)
    # Red-only and blue-only areas are unaffected by the order.
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 0), 0.5)
    assert_pixel_color(frame, (5, 5), (0, 0, 255))
    assert_pixel_alpha(mask, (5, 5), 0.5)

    frame = blue_first.get_frame(0.0)
    mask = blue_first.mask.get_frame(0.0)
    # Red is last now, so red is on top of blue in the overlap.
    assert_pixel_color(frame, (3, 3), (170, 0, 85))
    assert_pixel_alpha(mask, (3, 3), 0.75)
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 0), 0.5)
    assert_pixel_color(frame, (5, 5), (0, 0, 255))
    assert_pixel_alpha(mask, (5, 5), 0.5)

    # Alpha combines the same way regardless of the order.
    assert np.allclose(
        red_first.mask.get_frame(0.0), blue_first.mask.get_frame(0.0)
    )


def test_compositing_layer_index_overrides_list_order():
    """A higher ``layer_index`` wins even when the clip appears first."""
    size = (6, 6)
    low = (
        ColorClip((4, 4), (255, 0, 0), duration=2)
        .with_opacity(0.5)
        .with_position((0, 0))
        .with_layer_index(1)
    )
    high = (
        ColorClip((4, 4), (0, 0, 255), duration=2)
        .with_opacity(0.5)
        .with_position((2, 2))
        .with_layer_index(2)
    )

    # ``high`` is listed first but must still be painted last.
    composite = CompositeVideoClip([high, low], size=size, bg_color=None)
    assert [clip.layer_index for clip in composite.clips] == [1, 2]

    frame = composite.get_frame(0.0)
    mask = composite.mask.get_frame(0.0)
    assert_pixel_color(frame, (3, 3), (85, 0, 170))
    assert_pixel_alpha(mask, (3, 3), 0.75)
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_color(frame, (4, 4), (0, 0, 255))


def test_compositing_partial_overflow_keeps_background():
    """Clips hanging off any edge must not alter the uncovered background.

    The image of the overflowing clip is a horizontal red gradient, so using
    the wrong source slice produces a different color at the visible edge.
    """
    size = (10, 6)
    background_color = (20, 40, 60)

    columns = np.zeros((4, 6, 3), dtype=np.uint8)
    columns[:, :, 0] = (np.arange(6) * 40).astype(np.uint8)
    bottom = (
        ImageClip(columns, duration=2).with_opacity(0.5).with_position((-2, 1))
    )

    gradient = np.array(
        [
            [0.0, 0.4, 0.6, 0.8],
            [0.2, 0.5, 0.7, 0.9],
            [0.4, 0.6, 0.8, 1.0],
        ],
        dtype=float,
    )
    top = (
        ColorClip((4, 3), (0, 0, 200), duration=2)
        .with_mask(ImageClip(gradient, is_mask=True, duration=2))
        .with_position((7, 3))
    )

    composite = CompositeVideoClip(
        [ColorClip(size, background_color, duration=2), bottom, top],
        size=size,
    )
    frame = composite.get_frame(0.0)

    # Row above the bottom clip keeps the background untouched.
    assert_pixel_color(frame, (0, 0), background_color)
    assert_pixel_color(frame, (9, 0), background_color)

    # Bottom clip spans x=0..3 (it starts at x=-2), so the visible gradient
    # columns are its source columns 2..5.
    for x in range(4):
        expected = np.array([columns[0, x + 2, 0], 0, 0], dtype=float) * 0.5
        expected += np.array(background_color, dtype=float) * 0.5
        assert_pixel_color(frame, (x, 1), np.round(expected).astype(int))
    assert_pixel_color(frame, (4, 1), background_color)

    # The bottom clip covers rows 1..4 only.
    assert_pixel_color(frame, (0, 5), background_color)
    assert_pixel_color(frame, (0, 4), (50, 20, 30))

    # Top clip runs off the right edge: x=7..9 are its source columns 0..2.
    assert_pixel_color(frame, (7, 3), background_color)
    assert_pixel_color(frame, (8, 3), (12, 24, 116))
    assert_pixel_color(frame, (9, 3), (7, 15, 144))
    assert_pixel_color(frame, (8, 4), (10, 20, 130))
    assert_pixel_color(frame, (9, 5), (3, 7, 172))

    # With an opaque background the composite has no mask at all.
    assert composite.mask is None


def test_compositing_partial_overflow_corners():
    """A clip overflowing two edges at once keeps only the visible corner.

    The mask is a 4x4 ramp increasing both in x and in y, so every corner of
    the visible 2x2 region uses a distinct alpha value.
    """
    background_color = (100, 60, 20)
    size = (8, 8)
    alpha = np.arange(16, dtype=float).reshape(4, 4) / 16.0
    background = ColorClip(size, background_color, duration=2)
    clip = (
        ColorClip((4, 4), (0, 200, 0), duration=2)
        .with_mask(ImageClip(alpha, is_mask=True, duration=2))
    )

    # overflow top-left: canvas (0, 0) is the clip's source (2, 2).
    composite = CompositeVideoClip(
        [background, clip.with_position((-2, -2))], size=size
    )
    frame = composite.get_frame(0.0)
    for y in range(2):
        for x in range(2):
            expected = np.array([0, 200, 0], dtype=float) * alpha[y + 2, x + 2]
            expected += (
                np.array(background_color, dtype=float) * (1 - alpha[y + 2, x + 2])
            )
            assert_pixel_color(frame, (x, y), np.round(expected).astype(int))
    assert_pixel_color(frame, (2, 2), background_color)

    # overflow bottom-right: canvas (6, 6) is the clip's source (0, 0).
    composite = CompositeVideoClip(
        [background, clip.with_position((6, 6))], size=size
    )
    frame = composite.get_frame(0.0)
    for y in range(6, 8):
        for x in range(6, 8):
            expected = np.array([0, 200, 0], dtype=float) * alpha[y - 6, x - 6]
            expected += (
                np.array(background_color, dtype=float) * (1 - alpha[y - 6, x - 6])
            )
            assert_pixel_color(frame, (x, y), np.round(expected).astype(int))
    assert_pixel_color(frame, (5, 5), background_color)
    assert_pixel_color(frame, (0, 0), background_color)

    # The transparent background variant exposes exactly the same corner.
    transparent = CompositeVideoClip(
        [clip.with_position((6, 6))], size=size, bg_color=None
    )
    mask = transparent.mask.get_frame(0.0)
    assert np.allclose(mask[6:8, 6:8], alpha[0:2, 0:2])
    # Nothing leaks outside the 2x2 visible corner.
    assert np.all(mask[:6, :] == 0)
    assert np.all(mask[:, :6] == 0)


def test_compositing_mask_follows_clip_time_and_position():
    """The mask is sampled at clip-local time and composited at clip position.

    The mask alpha is ``0.1 + 0.2 * t`` in its own time base; the clip starts
    at 1s and its position is a function of its own time too. Each frame must
    therefore combine the clip-local alpha with the current position.
    """
    size = (12, 4)
    clip_start = 1.0

    def mask_frame(t):
        return np.full((3, 3), 0.1 + 0.2 * t)

    mask = VideoClip(frame_function=mask_frame, is_mask=True, duration=2)
    clip = (
        ColorClip((3, 3), (0, 200, 0), duration=2)
        .with_mask(mask)
        .with_start(clip_start)
        .with_position(lambda t: (int(2 * t) + 1, 1))
    )

    composite = CompositeVideoClip([clip], size=size, bg_color=None)

    # Not playing yet.
    before = composite.mask.get_frame(0.9)
    assert np.all(before == 0)

    for t in (1.0, 1.5, 2.0, 2.9):
        clip_local_t = t - clip_start
        x = int(2 * clip_local_t) + 1
        expected_alpha = 0.1 + 0.2 * clip_local_t
        frame_mask = composite.mask.get_frame(t)
        frame = composite.get_frame(t)

        # The mask covers exactly the 3x3 area at the current position.
        assert np.allclose(frame_mask[1:4, x : x + 3], expected_alpha)
        assert_pixel_color(frame, (x, 1), (0, 200, 0))

        # The columns to the left of the clip are still empty.
        assert np.all(frame_mask[1:4, 0:x] == 0)
        assert_pixel_color(frame, (0, 1), (0, 0, 0))

    # After the clip ends its mask is no longer part of the composite.
    after = composite.mask.get_frame(3.0)
    assert np.all(after == 0)

    # Over an opaque background the same clip-local alpha must drive the
    # color blend of ``compose_on``, not the composite time.
    background_color = (100, 0, 0)
    opaque = CompositeVideoClip(
        [
            ColorClip((6, 4), background_color, duration=3),
            (
                ColorClip((3, 3), (0, 200, 0), duration=2)
                .with_mask(
                    VideoClip(
                        frame_function=mask_frame, is_mask=True, duration=2
                    )
                )
                .with_start(clip_start)
                .with_position((1, 1))
            ),
        ],
        size=(6, 4),
    )
    assert opaque.mask is None

    for t in (1.0, 1.5, 2.0, 2.5):
        expected_alpha = 0.1 + 0.2 * (t - clip_start)
        expected = (
            np.array([0, 200, 0], dtype=float) * expected_alpha
            + np.array(background_color, dtype=float) * (1 - expected_alpha)
        )
        assert_pixel_color(opaque.get_frame(t), (1, 1), np.round(expected).astype(int))

    # The body itself is also sampled at clip-local time: it brightens with
    # each second, so using the composite time would overexpose it.
    def body_frame(t):
        value = np.uint8(10 + 20 * t)
        return np.full((3, 3, 3), value)

    animated_body = VideoClip(frame_function=body_frame, duration=2)
    animated = CompositeVideoClip(
        [
            ColorClip((6, 4), background_color, duration=3),
            (
                animated_body.with_mask(
                    VideoClip(
                        frame_function=mask_frame, is_mask=True, duration=2
                    )
                )
                .with_start(clip_start)
                .with_position((1, 1))
            ),
        ],
        size=(6, 4),
    )

    for t in (1.0, 1.5, 2.0):
        clip_local_t = t - clip_start
        expected_alpha = 0.1 + 0.2 * clip_local_t
        body_value = 10 + 20 * clip_local_t
        expected = (
            np.array([body_value] * 3, dtype=float) * expected_alpha
            + np.array(background_color, dtype=float) * (1 - expected_alpha)
        )
        assert_pixel_color(
            animated.get_frame(t), (1, 1), np.round(expected).astype(int)
        )


def test_compositing_fully_transparent_region_reveals_background():
    """Fully transparent mask holes show the background, not a blend."""
    size = (5, 5)
    hidden = np.array(
        [[1.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 1.0]], dtype=float
    )
    background_color = (255, 0, 0)
    foreground = (
        ColorClip((3, 3), (0, 0, 255), duration=2)
        .with_mask(ImageClip(hidden, is_mask=True, duration=2))
        .with_position((1, 1))
    )

    composite = CompositeVideoClip(
        [ColorClip(size, background_color, duration=2), foreground], size=size
    )
    frame = composite.get_frame(0.0)

    for y in range(3):
        for x in range(3):
            canvas_x, canvas_y = x + 1, y + 1
            if hidden[y, x] == 1.0:
                assert_pixel_color(frame, (canvas_x, canvas_y), (0, 0, 255))
            else:
                assert_pixel_color(
                    frame, (canvas_x, canvas_y), background_color
                )

    # Border pixels are untouched.
    for x in range(5):
        assert_pixel_color(frame, (x, 0), background_color)
        assert_pixel_color(frame, (x, 4), background_color)

    # The same content over a transparent background exposes the same holes.
    transparent = CompositeVideoClip(
        [foreground], size=size, bg_color=None
    )
    mask = transparent.mask.get_frame(0.0)
    expected = np.zeros((5, 5), dtype=float)
    expected[1:4, 1:4] = hidden
    assert np.allclose(mask, expected)


def test_compositing_crop_does_not_change_audio_interval():
    """Cropping the picture must leave the audio track untouched.

    The audio activity window (start/end/duration) and the samples played
    while the clip is active must be identical before and after cropping,
    including when the cropped clip is used inside a composite. The signal is
    a ramp, so a time shift inside the active window would also be caught.
    """

    def ramp(t):
        t = np.atleast_1d(np.asarray(t, dtype=float))
        return 0.1 + 0.2 * t

    audio = AudioClip(ramp, duration=2, fps=100)

    size = (6, 6)
    body = (
        ColorClip(size, (10, 20, 30), duration=2)
        .with_audio(audio)
        .with_start(1.0)
    )
    cropped = body.cropped(x1=1, y1=2, x2=5, y2=6)

    # The video geometry did change...
    assert body.size == (6, 6)
    assert cropped.size == (4, 4)

    # ...but the audio is the very same activity window.
    assert cropped.audio is not None
    assert cropped.audio.start == body.audio.start == 1.0
    assert cropped.audio.end == body.audio.end == 3.0
    assert cropped.audio.duration == body.audio.duration == 2

    for t in np.arange(0, 2, 0.1):
        assert np.allclose(
            np.atleast_1d(cropped.audio.get_frame(t)),
            np.atleast_1d(body.audio.get_frame(t)),
        )

    expected_samples = {
        1.0: 0.1,
        1.5: 0.2,
        2.0: 0.3,
        2.99: 0.1 + 0.2 * 1.99,
    }

    background = ColorClip((8, 6), (0, 0, 0), duration=4)
    for label, clip in [("uncropped", body), ("cropped", cropped)]:
        composite = CompositeVideoClip(
            [background, clip.with_position((2, 1))], size=(8, 6)
        )
        assert composite.audio is not None, label
        assert composite.audio.start == 0
        assert composite.audio.end == 3.0

        # Silent before the clip starts, active during its window and silent
        # again after it ends.
        assert np.allclose(composite.audio.get_frame(0.5), 0.0), label
        assert np.allclose(composite.audio.get_frame(0.99), 0.0), label
        for t, value in expected_samples.items():
            assert np.allclose(
                composite.audio.get_frame(t), value
            ), f"{label} at t={t}"
        assert np.allclose(composite.audio.get_frame(3.0), 0.0), label
        assert np.allclose(composite.audio.get_frame(3.5), 0.0), label


def test_compositing_cropped_clip_keeps_mask_in_place():
    """Cropping shifts the body and its mask together inside a composite.

    The body is an asymmetric color ramp and the mask a ramp over the same
    grid, so shifting only one of them would produce different pixels.
    """
    height = width = 6
    yy, xx = np.mgrid[0:height, 0:width]
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[..., 0] = (xx * 30 + 10).astype(np.uint8)
    image[..., 1] = (yy * 30 + 5).astype(np.uint8)
    image[..., 2] = 80

    alpha = np.arange(height * width, dtype=float).reshape(height, width) / 36.0

    body = (
        ImageClip(image, duration=3)
        .with_mask(ImageClip(alpha, is_mask=True, duration=3))
        .with_start(1.0)
    )
    cropped = body.cropped(x1=1, y1=2, x2=5, y2=6)

    # The mask is cropped with the same window as the image.
    assert cropped.size == (4, 4)
    assert cropped.mask.size == (4, 4)
    assert np.allclose(cropped.get_frame(0.0), image[2:6, 1:5])
    assert np.allclose(cropped.mask.get_frame(0.0), alpha[2:6, 1:5])
    # The time window is untouched by cropping.
    assert (cropped.start, cropped.end) == (1.0, 4.0)
    assert (cropped.mask.start, cropped.mask.end) == (1.0, 4.0)

    composite = CompositeVideoClip(
        [cropped.with_position((1, 1))], size=(8, 8), bg_color=None
    )

    # Before the clip starts the canvas is empty.
    frame_mask = composite.mask.get_frame(0.5)
    assert np.all(frame_mask == 0)

    frame = composite.get_frame(1.5)
    frame_mask = composite.mask.get_frame(1.5)

    # Body and mask land on the same canvas region.
    assert np.allclose(frame[1:5, 1:5], image[2:6, 1:5])
    assert np.allclose(frame_mask[1:5, 1:5], alpha[2:6, 1:5])
    assert_pixel_color(frame, (1, 1), image[2, 1])
    assert_pixel_alpha(frame_mask, (1, 1), alpha[2, 1])

    # Nothing leaks outside the cropped region.
    assert_only_region_has_alpha(frame_mask, (1, 5, 1, 5))
    assert np.all(frame[0, 0] == 0)
    assert np.all(frame[7, 7] == 0)


def test_compositing_combined_dynamic_layers_and_overflow():
    """Integrated check mixing layers, dynamic positions and overflow.

    Layer 0 is a static red band, layer 1 holds a green clip sliding to the
    right and a blue clip appearing after 1s at the same layer, and layer 2
    is a white clip that hangs off the bottom-right corner. The alpha grid
    acts as a coverage map: each cell must equal ``1 - 0.5 ** n`` where ``n``
    is the number of half-transparent layers covering it.
    """
    size = (10, 8)
    base = (
        ColorClip((10, 4), (255, 0, 0), duration=3)
        .with_opacity(0.5)
        .with_layer_index(0)
    )
    mover = (
        ColorClip((6, 4), (0, 255, 0), duration=3)
        .with_opacity(0.5)
        .with_position(lambda t: (int(4 * t) - 2, 3))
        .with_layer_index(1)
    )
    blue = (
        ColorClip((4, 4), (0, 0, 255), duration=1)
        .with_opacity(0.5)
        .with_position((3, 3))
        .with_layer_index(1)
        .with_start(1)
    )
    white = (
        ColorClip((4, 4), (255, 255, 255), duration=3)
        .with_opacity(0.5)
        .with_position((8, 5))
        .with_layer_index(2)
    )

    composite = CompositeVideoClip(
        [base, mover, blue, white], size=size, bg_color=None
    )

    assert composite.duration == 3
    assert [clip.layer_index for clip in composite.clips] == [0, 1, 1, 2]

    # --- t=0.5, mover at x=0, blue not playing yet -----------------------
    frame = composite.get_frame(0.5)
    mask = composite.mask.get_frame(0.5)

    # Red band only.
    assert_pixel_color(frame, (0, 0), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 0), 0.5)
    # Red + green.
    assert_pixel_color(frame, (0, 3), (85, 170, 0))
    assert_pixel_alpha(mask, (0, 3), 0.75)
    # Green only, below the red band.
    assert_pixel_color(frame, (2, 5), (0, 255, 0))
    assert_pixel_alpha(mask, (2, 5), 0.5)
    # White hangs off the corner: its visible part starts at x=8, y=5.
    assert_pixel_color(frame, (8, 5), (255, 255, 255))
    assert_pixel_alpha(mask, (8, 5), 0.5)
    assert_pixel_alpha(mask, (7, 5), 0.0)
    assert_pixel_color(frame, (7, 5), (0, 0, 0))

    # --- t=1.5, mover at x=4, blue playing ------------------------------
    frame = composite.get_frame(1.5)
    mask = composite.mask.get_frame(1.5)

    # Three layers overlap at (5, 3) and (6, 3): red + green + blue.
    assert_pixel_color(frame, (5, 3), (36, 73, 146))
    assert_pixel_alpha(mask, (5, 3), 0.875)
    assert_pixel_color(frame, (6, 3), (36, 73, 146))
    assert_pixel_alpha(mask, (6, 3), 0.875)
    # Red + blue at (3, 3).
    assert_pixel_color(frame, (3, 3), (85, 0, 170))
    assert_pixel_alpha(mask, (3, 3), 0.75)
    # Red only left of the moving green clip.
    assert_pixel_color(frame, (0, 3), (255, 0, 0))
    assert_pixel_alpha(mask, (0, 3), 0.5)
    # Blue only below the red band, left of the moving green clip.
    assert_pixel_color(frame, (3, 5), (0, 0, 255))
    assert_pixel_alpha(mask, (3, 5), 0.5)
    # Green + blue below the red band.
    assert_pixel_color(frame, (4, 4), (0, 85, 170))
    assert_pixel_alpha(mask, (4, 4), 0.75)
    # Green only below the red band, right of the blue clip.
    assert_pixel_color(frame, (7, 5), (0, 255, 0))
    assert_pixel_alpha(mask, (7, 5), 0.5)
    # Green + white where both hang out of the red band.
    assert_pixel_color(frame, (8, 5), (170, 255, 170))
    assert_pixel_alpha(mask, (8, 5), 0.75)

    # --- t=2.5, mover almost gone to the right, blue ended -----------------
    frame = composite.get_frame(2.5)
    mask = composite.mask.get_frame(2.5)

    # Green is now at x=8..9, so only two columns of it remain.
    assert_pixel_color(frame, (8, 3), (85, 170, 0))
    assert_pixel_alpha(mask, (8, 3), 0.75)
    assert_pixel_color(frame, (9, 3), (85, 170, 0))
    assert_pixel_alpha(mask, (9, 3), 0.75)
    # Left of the green clip the red band is uncovered again.
    assert_pixel_color(frame, (5, 3), (255, 0, 0))
    assert_pixel_alpha(mask, (5, 3), 0.5)
    # Blue ended, so the area below the red band is transparent again.
    assert_pixel_color(frame, (2, 5), (0, 0, 0))
    assert_pixel_alpha(mask, (2, 5), 0.0)
    # White still hangs off the corner.
    assert_pixel_color(frame, (8, 5), (170, 255, 170))
    assert_pixel_alpha(mask, (8, 5), 0.75)
    assert_pixel_color(frame, (9, 7), (255, 255, 255))
    assert_pixel_alpha(mask, (9, 7), 0.5)

    # --- Whole-canvas alpha equals the layer count formula -----------------
    # For each timestamp, ``n`` is the number of half-transparent layers
    # covering a pixel, so its alpha must be ``1 - 0.5 ** n``.
    def expected_alpha(rects):
        layers = np.zeros((8, 10), dtype=int)
        for x, y, w, h in rects:
            x1, x2 = max(x, 0), min(x + w, 10)
            y1, y2 = max(y, 0), min(y + h, 8)
            if x1 < x2 and y1 < y2:
                layers[y1:y2, x1:x2] += 1
        return 1 - 0.5**layers

    assert np.allclose(
        composite.mask.get_frame(0.5),
        expected_alpha([(0, 0, 10, 4), (0, 3, 6, 4), (8, 5, 4, 4)]),
    )
    assert np.allclose(
        composite.mask.get_frame(1.5),
        expected_alpha(
            [(0, 0, 10, 4), (4, 3, 6, 4), (3, 3, 4, 4), (8, 5, 4, 4)]
        ),
    )
    assert np.allclose(
        composite.mask.get_frame(2.5),
        expected_alpha([(0, 0, 10, 4), (8, 3, 6, 4), (8, 5, 4, 4)]),
    )


def test_concatenate_with_masks(util):
    video_without_mask = (
        ColorClip(size=(10, 10), color=(255, 0, 0)).with_duration(1).with_fps(1)
    )
    video_with_mask = (
        ColorClip(size=(5, 5), color=(0, 255, 0))
        .with_duration(1)
        .with_fps(1)
        .with_mask()
    )

    output = os.path.join(util.TMP_DIR, "test_concatenate_with_masks.mp4")
    concatenate_videoclips([video_without_mask, video_with_mask]).write_videofile(
        output
    )


if __name__ == "__main__":
    pytest.main()
