"""extract_pcm_segment must cut on whole int16 samples (offline: python3 test_extract_alignment.py)."""
import array
import unittest

from audio_utils import extract_pcm_segment


class AlignmentTest(unittest.TestCase):
    def test_any_start_time_gives_whole_samples(self):
        pcm = array.array("h", range(-8000, 8000)).tobytes()          # 1 s at 16 kHz
        for start in (0.0, 0.10003, 0.123456, 0.5000312, 0.99997):
            seg = extract_pcm_segment(pcm, 16000, start, 1.0)
            self.assertEqual(len(seg) % 2, 0)
            first = array.array("h", seg[:2])[0]
            self.assertEqual(first, -8000 + int(start * 16000), start)


if __name__ == "__main__":
    unittest.main()
