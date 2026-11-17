"""Tests for mixing several AudioClips through ``CompositeAudioClip``.

These tests focus on how the activity intervals (``start``/``end``) of the
mixed clips and the channel broadcasting rules jointly determine the output:

- Mono and stereo clips with different start/end times are mixed together;
  a mono clip contributes its amplitude equally to every output channel.
- A scalar time request and a batch (array of times) request must express
  the same sound superposition.
- Silence must not be polluted by material that has already ended or has
  not started yet.
- Batch requests crossing clip boundaries (or a short tail chunk at the
  end of the output) must keep channel positions and amplitudes consistent
  with each clip's activity interval, and must not be confused with
  out-of-bounds reads from the underlying clips.

All clips are built locally from Numpy arrays or plain functions, so the
tests require neither media files nor network access.
"""

import numpy as np

import pytest

from moviepy.audio.AudioClip import AudioArrayClip, AudioClip, CompositeAudioClip


FPS = 10

# Amplitudes are dyadic fractions so that sums are exact in floating point
# and the assertions can use strict equality.
MONO_VALUE = 0.5
STEREO_LEFT = 0.25
STEREO_RIGHT = -0.25

# Activity intervals (seconds):
#   mono:   [0.5, 1.5)
#   stereo: [0.0, 2.0)
MONO_START = 0.5
MONO_DURATION = 1.0
STEREO_DURATION = 2.0


def make_mono_clip():
    """Mono clip of constant amplitude, active on [0.5, 1.5)."""
    array = MONO_VALUE * np.ones((int(MONO_DURATION * FPS), 1))
    return AudioArrayClip(array, fps=FPS).with_start(MONO_START)


def make_stereo_clip():
    """Stereo clip of constant amplitude, active on [0, 2.0)."""
    array = np.column_stack(
        [
            STEREO_LEFT * np.ones(int(STEREO_DURATION * FPS)),
            STEREO_RIGHT * np.ones(int(STEREO_DURATION * FPS)),
        ]
    )
    # ``with_duration`` also sets ``end`` so the composite can compute its
    # own duration from the activity intervals of the mixed clips
    return AudioArrayClip(array, fps=FPS).with_duration(STEREO_DURATION)


def make_composite():
    """Stereo composite of the mono and the stereo clip above."""
    return CompositeAudioClip([make_mono_clip(), make_stereo_clip()])


def expected_frame(t):
    """Expected 2-channels frame of the composite at scalar time ``t``."""
    left = right = 0.0
    if 0.0 <= t < STEREO_DURATION:
        left += STEREO_LEFT
        right += STEREO_RIGHT
    if MONO_START <= t < MONO_START + MONO_DURATION:
        # the mono amplitude is broadcast to every output channel
        left += MONO_VALUE
        right += MONO_VALUE
    return np.array([left, right])


def test_composite_setup_attributes():
    """The composite takes its channels and duration from the mixed clips."""
    composite = make_composite()

    assert composite.nchannels == 2
    assert composite.duration == STEREO_DURATION
    assert composite.fps == FPS


def test_activity_intervals_determine_scalar_output():
    """Each scalar time must render only the clips active at that time:
    before the mono clip starts only the stereo one is heard, inside the
    overlap both are summed, after the mono clip ends only the stereo one
    remains, and past every end the output is exact silence.
    """
    composite = make_composite()

    # only the stereo clip is playing
    np.testing.assert_array_equal(
        composite.get_frame(0.2), [STEREO_LEFT, STEREO_RIGHT]
    )
    # overlap: the mono amplitude is added to both channels
    np.testing.assert_array_equal(
        composite.get_frame(1.0), [STEREO_LEFT + MONO_VALUE, STEREO_RIGHT + MONO_VALUE]
    )
    # the mono clip has ended, only the stereo one remains
    np.testing.assert_array_equal(
        composite.get_frame(1.7), [STEREO_LEFT, STEREO_RIGHT]
    )
    # everything has ended: exact silence, nothing leaks from the clips
    np.testing.assert_array_equal(composite.get_frame(2.0), [0.0, 0.0])
    np.testing.assert_array_equal(composite.get_frame(5.0), [0.0, 0.0])


def test_mono_amplitude_is_broadcast_to_every_stereo_channel():
    """A mono clip mixed into a stereo composite adds its amplitude,
    unchanged, to the left and to the right channel alike.
    """
    composite = make_composite()

    frame = composite.get_frame(1.0)

    assert frame[0] - STEREO_LEFT == MONO_VALUE
    assert frame[1] - STEREO_RIGHT == MONO_VALUE
    # both channels received exactly the same contribution
    assert frame[0] - STEREO_LEFT == frame[1] - STEREO_RIGHT


def test_scalar_and_batch_requests_express_same_superposition():
    """Requesting the frames one scalar at a time or as one batch of times
    must give the same samples, including at the exact start/end boundaries
    of the mixed clips: at those instants the array-backed clips read
    outside their data and legitimately render zeros, so both request
    styles agree.
    """
    composite = make_composite()

    # the grid deliberately hits the clips boundaries (0.5, 1.5, 2.0)
    # and goes past the end of the composition
    times = np.arange(0, 25) / 10.0

    batch = composite.get_frame(times)

    assert batch.shape == (len(times), 2)
    for i, t in enumerate(times):
        scalar = np.asarray(composite.get_frame(float(t)), dtype=float)
        np.testing.assert_array_equal(
            batch[i],
            scalar,
            err_msg=f"scalar and batch requests disagree at t={t}",
        )
        np.testing.assert_array_equal(
            batch[i], expected_frame(float(t)), err_msg=f"wrong mix at t={t}"
        )


def test_batch_request_spanning_boundaries_keeps_channels_and_amplitudes():
    """A single batch request whose times cross the start of one clip and
    the end of another must place each valid sample in the right channel
    with the amplitude dictated by the activity intervals, and render
    exact zeros where no clip is playing.
    """
    composite = make_composite()

    # crosses the mono start (0.5), the mono end (1.5) and the stereo
    # end (2.0) in one single request
    times = np.array([0.0, 0.4, 0.5, 0.9, 1.4, 1.5, 1.9, 2.0, 2.3])
    batch = composite.get_frame(times)

    expected = np.array([expected_frame(float(t)) for t in times])
    np.testing.assert_array_equal(batch, expected)

    # channel positions: left column holds left-channel sums, right column
    # right-channel sums, even for samples surrounded by silence
    np.testing.assert_array_equal(batch[:, 0], expected[:, 0])
    np.testing.assert_array_equal(batch[:, 1], expected[:, 1])
    # samples outside every activity interval are exactly zero
    assert (batch[-2:] == 0.0).all()


def test_short_tail_chunk_keeps_valid_samples():
    """When the output is rendered by chunks and the last chunk is smaller
    than the requested chunk size, the samples of that short tail still
    follow the activity intervals of the mixed clips.
    """
    composite = make_composite()

    chunksize = 8  # does not divide the 20 samples of the composition
    chunks = list(composite.iter_chunks(chunksize=chunksize, fps=FPS, logger=None))

    # the tail chunk is shorter than the requested chunk size
    assert 0 < chunks[-1].shape[0] < chunksize
    assert sum(chunk.shape[0] for chunk in chunks) == int(STEREO_DURATION * FPS)

    rendered = np.vstack(chunks)
    expected = np.array(
        [expected_frame(i / FPS) for i in range(int(STEREO_DURATION * FPS))]
    )
    np.testing.assert_array_equal(rendered, expected)

    # the short tail on its own is still correct, channel by channel
    tail = chunks[-1]
    tail_start = rendered.shape[0] - tail.shape[0]
    np.testing.assert_array_equal(tail, expected[tail_start:])


def test_inactive_clips_are_never_read_for_scalar_requests():
    """For scalar times outside a clip's activity interval the clip is
    excluded from the mix without even being read: its frame function is
    not called, so ended or not-yet-started material cannot pollute the
    silence.
    """
    requested_times = []

    def spying_frame_function(t):
        requested_times.append(t)
        # garbage that must never reach the output outside [1, 2)
        return np.array([7.0, 7.0])

    clip = AudioClip(spying_frame_function, duration=1.0, fps=FPS).with_start(1.0)
    composite = CompositeAudioClip([clip])

    requested_times.clear()  # ignore the probe frame read at construction

    # before the clip starts and after it ends: exact silence...
    np.testing.assert_array_equal(composite.get_frame(0.5), [0.0, 0.0])
    np.testing.assert_array_equal(composite.get_frame(2.0), [0.0, 0.0])
    np.testing.assert_array_equal(composite.get_frame(3.5), [0.0, 0.0])
    # ...produced without reading the clip at those times at all
    assert requested_times == []

    # inside the activity interval the clip is read, relative to its start
    np.testing.assert_array_equal(composite.get_frame(1.5), [7.0, 7.0])
    assert requested_times == [0.5]


def test_batch_request_crossing_boundary_reads_clip_out_of_bounds():
    """Reveals the conflict between mixing and the read boundaries of the
    underlying clips: when a batch request straddles a clip's activity
    interval, the composite applies the activity mask *after* reading, so
    the clip's frame function receives the out-of-range times too.

    A strict reader that rejects out-of-bounds times works fine with
    scalar requests (inactive times are never read) but fails on a batch
    request crossing the boundary, while a tolerant, array-backed clip
    renders the same batch as legitimate silence plus valid superposition
    (see ``test_scalar_and_batch_requests_express_same_superposition``).
    """

    def strict_frame_function(t):
        tt = np.atleast_1d(np.asarray(t, dtype=float))
        if ((tt < 0.0) | (tt >= 1.0)).any():
            raise ValueError("out-of-bounds read")
        if isinstance(t, np.ndarray):
            return np.full((len(t), 2), 0.5)
        return np.array([0.5, 0.5])

    strict_clip = AudioClip(strict_frame_function, duration=1.0, fps=FPS)
    composite = CompositeAudioClip([strict_clip])

    # scalar requests across the boundary are fine: out-of-interval times
    # are excluded without reading the clip
    np.testing.assert_array_equal(composite.get_frame(0.5), [0.5, 0.5])
    np.testing.assert_array_equal(composite.get_frame(1.5), [0.0, 0.0])

    # but a batch request crossing the end of the clip propagates the
    # out-of-bounds read failure of the underlying strict reader
    with pytest.raises(ValueError, match="out-of-bounds read"):
        composite.get_frame(np.array([0.5, 1.5]))

    # the very same batch is a legitimate mix of sound and silence for a
    # tolerant array-backed clip with the same activity interval
    tolerant = CompositeAudioClip(
        [AudioArrayClip(0.5 * np.ones((FPS, 2)), fps=FPS)]
    )
    np.testing.assert_array_equal(
        tolerant.get_frame(np.array([0.5, 1.5])), [[0.5, 0.5], [0.0, 0.0]]
    )


def test_mono_frame_function_must_return_column_vectors_for_batches():
    """Channel broadcasting requires mono frame functions to return a
    column vector of shape ``(n, 1)`` for batch requests. A function
    returning a flat ``(n,)`` array (like a plain ``np.sin``) works for
    scalar requests but breaks the batch broadcasting against the
    ``(n, 1)`` activity mask.
    """

    def flat_mono_frame(t):
        if isinstance(t, np.ndarray):
            return MONO_VALUE * np.ones(len(t))  # shape (n,)
        return MONO_VALUE

    def column_mono_frame(t):
        if isinstance(t, np.ndarray):
            return MONO_VALUE * np.ones((len(t), 1))  # shape (n, 1)
        return np.array([MONO_VALUE])

    stereo = make_stereo_clip()

    flat_composite = CompositeAudioClip(
        [AudioClip(flat_mono_frame, duration=1.0, fps=FPS), stereo]
    )
    # scalar requests broadcast the scalar mono value without trouble
    expected = [STEREO_LEFT + MONO_VALUE, STEREO_RIGHT + MONO_VALUE]
    np.testing.assert_array_equal(flat_composite.get_frame(0.5), expected)
    # batch requests cannot broadcast (n,) against the (n, 1) mask
    # (3 times so the (n, n) result cannot accidentally match the
    # 2-channels output shape)
    with pytest.raises(ValueError, match="broadcast"):
        flat_composite.get_frame(np.array([0.2, 0.5, 0.8]))

    # with column vectors, scalar and batch requests agree again
    column_composite = CompositeAudioClip(
        [AudioClip(column_mono_frame, duration=1.0, fps=FPS), make_stereo_clip()]
    )
    times = np.array([0.2, 0.5, 1.5])
    batch = column_composite.get_frame(times)
    for i, t in enumerate(times):
        np.testing.assert_array_equal(
            batch[i], np.asarray(column_composite.get_frame(float(t)), dtype=float)
        )


if __name__ == "__main__":
    pytest.main()
