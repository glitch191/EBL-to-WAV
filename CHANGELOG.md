# Changelog

## 1.0.0 (2026-10-14)

First public release.

- Converts E-mu Emulator X samples (`.ebl`) to 16-bit WAV or FLAC files.
- Accepts `.exb` banks (their `SamplePool` folder is converted), `.ebl` files, folders and
  `.zip` / `.7z` archives (unpacked automatically); several at once.
- Scan first, then confirm: the list shows every sample with its bank, category, channels,
  sample rate, length and loop, and whether it is **New** or will **Replace** an existing file.
- WAV files keep the sample's loop points (`smpl` chunk), so samplers loop them as Emulator X did.
- Stereo samples are rebuilt from their two channels, including files with a padding sample
  between the channels.
- One folder per bank, named after the `.exb` file; an archive keeps its folder layout under a
  folder named after the archive. Re-converting replaces earlier output and never touches files
  added by hand.
