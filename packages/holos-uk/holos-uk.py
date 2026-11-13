# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "holos-tts-mlx==0.1",
#   "ukrainian-word-stress @ git+https://github.com/patriotyk/ukrainian-word-stress@d5b37ea0abe9711930d9c3b0b13edec5a0675512",
#   "ipa-uk @ git+https://github.com/patriotyk/ipa-uk@3faf05ec50fd4880965b6c76f8e479a4f82117f5",
# ]
# ///
"""Ukrainian text file to MP3 with HolosTTS on MLX (Apple silicon GPU).

Usage: holos-uk IN.txt OUT.mp3 [--voice NAME] [--speed 1.0] [--pause 0.4]
       holos-uk --list-voices

The first run installs the dependencies (PyTorch, for stress marks) and
downloads the model (patriotyk/HolosTTS/mlx) into the Hugging Face cache.
Needs ffmpeg in PATH.
"""

import argparse
import re
import subprocess
import sys

import numpy as np
from holos_mlx import HolosTTS

p = argparse.ArgumentParser(prog="holos-uk")
p.add_argument("input", nargs="?")
p.add_argument("output", nargs="?")
p.add_argument("--voice")
p.add_argument("--speed", type=float, default=1.0)
p.add_argument("--pause", type=float, default=0.4, help="seconds between sentences")
p.add_argument("--list-voices", action="store_true")
a = p.parse_args()

model = HolosTTS()
if a.list_voices:
    print("\n".join(model.list_voices()))
    sys.exit()
if not (a.input and a.output):
    p.error("input and output are required")

from ipa_uk import ipa  # noqa: E402
from ukrainian_word_stress import Stressifier  # noqa: E402

stressify = Stressifier()
voice = a.voice or model.list_voices()[0]
sr = model.sample_rate
gap = np.zeros(int(sr * a.pause), dtype=np.float32)

with open(a.input, encoding="utf-8") as f:
    lines = f.read().splitlines()
sentences = [s for line in lines for s in re.split(r"(?<=[.!?…])\s+", line.strip()) if s]

parts = []
for i, s in enumerate(sentences, 1):
    audio = model.generate(ipa(stressify(s)), voice=voice, speed=a.speed)
    parts += [np.asarray(audio, dtype=np.float32).reshape(-1), gap]
    print(f"\r{i}/{len(sentences)}", end="", file=sys.stderr, flush=True)
print(file=sys.stderr)

pcm = np.concatenate(parts).astype("<f4").tobytes()
subprocess.run(
    ["ffmpeg", "-loglevel", "error", "-n", "-f", "f32le", "-ar", str(sr), "-ac", "1",
     "-i", "pipe:0", "-b:a", "64k", a.output],
    input=pcm,
    check=True,
)
