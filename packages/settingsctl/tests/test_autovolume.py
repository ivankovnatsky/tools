"""Tests for the AutoVolume daemon feature (probes and volume are faked)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import autovolume  # noqa: E402


class AutoVolumeTickTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.volume = 0
        self.sets = []

        def volume_set(percent):
            self.sets.append(percent)
            self.volume = percent
            return True

        for target, value in (
            ("time.monotonic", lambda: self.now),
            ("autovolume.is_audio_active", lambda: False),
            ("volume.volume_get", lambda: self.volume),
            ("volume.volume_set", volume_set),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def idle_tick(self, feature):
        self.now += feature.idle_seconds
        feature.tick()

    def make(self, threshold=6, raise_below=False):
        return autovolume.AutoVolume(60, threshold, verbose=False, raise_below=raise_below)

    def test_lowers_above_threshold(self):
        self.volume = 40
        self.idle_tick(self.make(raise_below=True))
        self.assertEqual(self.sets, [6])

    def test_below_threshold_left_alone_by_default(self):
        self.idle_tick(self.make())
        self.assertEqual(self.sets, [])

    def test_raise_below_threshold(self):
        self.idle_tick(self.make(raise_below=True))
        self.assertEqual(self.sets, [6])

    def test_raise_capped_at_max(self):
        self.idle_tick(self.make(threshold=30, raise_below=True))
        self.assertEqual(self.sets, [autovolume.RAISE_MAX_PERCENT])

    def test_between_cap_and_threshold_untouched(self):
        self.volume = 20
        self.idle_tick(self.make(threshold=30, raise_below=True))
        self.assertEqual(self.sets, [])

    def test_not_before_idle(self):
        feature = self.make(raise_below=True)
        feature.tick()
        self.assertEqual(self.sets, [])

    def test_once_per_idle_period(self):
        feature = self.make(raise_below=True)
        self.idle_tick(feature)
        self.volume = 0
        self.idle_tick(feature)
        self.assertEqual(self.sets, [6])


if __name__ == "__main__":
    unittest.main()
