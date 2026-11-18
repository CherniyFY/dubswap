# dubswap

Merge an external audio track and subtitles into MKV episodes.

For every video in a folder the tool finds exactly one audio file and one
subtitle file with the same episode number, then remuxes them together with the
original tracks and the subtitle fonts:

- the new audio and subtitle tracks become the default tracks,
- the original audio and subtitle tracks keep their data but lose the default
  flag, so players pick the new ones,
- fonts from `FOLDER/Fonts` are attached as MKV attachments,
- the source files are never modified.

A merged file appears in the output folder only after a successful `mkvmerge`
run and verification. If a run fails or is interrupted after writing starts,
its `.part` file stays in a private staging directory for inspection.

## Requirements

- Python 3.8 or newer
- An output filesystem that supports hard links when running without `--force`.
- A recent MKVToolNix with `mkvmerge` on the PATH, or pass `--mkvmerge PATH`.
  macOS installs to `/Applications/MKVToolNix.app/Contents/MacOS/mkvmerge`,
  which is found automatically.
- The tool uses `mkvmerge --identify --identification-format json` and
  `--default-track-flag`, so very old MKVToolNix releases will not work. To
  check yours: `mkvmerge --identify --identification-format json` should be
  accepted rather than rejected as an unknown option.

## Usage

```sh
./dubswap.py "/path/to/season 1" --lang eng --audio-name "English dub"
```

Default layout, everything below `FOLDER`:

```text
FOLDER/
  Show S01E01 [1080p].mkv        source videos, not touched
  Dub - 01.mka                   audio, matched by episode number
  Signs - 01.ass                 subtitles, matched by episode number
  Fonts/                          attachments for the signs
  merged/                         output
```

Other layouts are described on the command line:

```sh
./dubswap.py "season 1" \
  --audio-dir "audio/english" --audio '*.mka' \
  --subs-dir  "subs/english"  --subs  '*.ass' \
  --fonts     "subs/fonts" \
  --lang eng --audio-name "English dub" --subs-name "Signs"
```

Check what would run before writing anything:

```sh
./dubswap.py "season 1" --dry-run
```

Audio only, and a second output folder:

```sh
./dubswap.py "season 1" --no-subs --out "merged english"
```

| Option | Meaning |
| --- | --- |
| `--audio GLOB` | audio file names to look for, repeatable (default `*.mka`) |
| `--subs GLOB` | subtitle file names to look for, repeatable (default `*.ass`, `*.ssa`) |
| `--no-subs` | merge audio only |
| `--audio-dir DIR` | where the audio files are, below FOLDER (default FOLDER) |
| `--subs-dir DIR` | where the subtitles are, below FOLDER (default FOLDER) |
| `--fonts DIR` | font folder, repeatable (default `FOLDER/Fonts`) |
| `--lang CODE` | language code for the new tracks, e.g. `eng` |
| `--audio-name NAME` | track name for the new audio |
| `--subs-name NAME` | track name for the new subtitles |
| `--episode-regex REGEX` | episode number regex with capture groups, repeatable |
| `--out DIR` | output folder, below FOLDER (default `FOLDER/merged`) |
| `--suffix TEXT` | added to output file names (default `.merged`). Path separators are disallowed |
| `--keep-original-defaults` | leave the default flag on the original tracks |
| `--force` | overwrite existing output files |
| `--dry-run` | print the `mkvmerge` commands, write nothing |
| `--mkvmerge PATH` | use this binary |

## How files are matched

Episode numbers are read from file names. By default these patterns are tried
in order, and the first one that matches wins:

| Pattern | Example | Key |
| --- | --- | --- |
| `S01E03` | `Show S01E03 [1080p].mkv` | `1-3` |
| `E03`, `EP03` | `Show EP03.mka` | `3` |
| ` - 03` | `Show - 03 [1080p].mkv` | `3` |

Keys are compared as numbers, so `03` and `3` are the same episode. Keys of
different length match when the shorter one is a suffix of the longer one, so a
video named `S01E03` pairs with an audio file named `- 03` and not with `- 13`
or `- 103`.

Use `--episode-regex` for anything else. Every pattern needs at least one
capture group; the captured numbers form the key:

```sh
./dubswap.py "season 1" --episode-regex '_(\d{2})_' --audio-dir audio
```

A video is skipped, with the reason printed, when its own name has no episode
number, when no asset matches, or when more than one asset matches. Narrowing
`--audio` or `--subs` fixes the ambiguity.

## Safety

- The tool refuses to use an input file as an output, even with `--force`.
- Each merge uses a private staging directory. The tool publishes the result
  after `mkvmerge` exits 0 or 1 (1 means "finished with warnings") and the
  file re-identifies with the expected number of tracks.
- Existing outputs are left alone unless `--force` is given. Without it,
  concurrent runs may merge the same episode, and the first to publish wins.
- Exit codes: `0` everything merged or already present, `1` something failed or
  had no matching file, `2` the run could not start (missing folder, missing
  `mkvmerge`, bad regex).
- `mkvmerge` messages are read as JSON with a C locale, so translated
  MKVToolNix output cannot break parsing.

## Notes and limits

- Chapter and tag handling is whatever `mkvmerge` does by default; nothing is
  rewritten for the new tracks.
- A failed merge prints the path to any remaining `.part` file. You can remove
  its staging directory after inspection. Each retry uses a fresh directory.
- A dry run prints commands with `.dubswap-TEMP/output.part` as the temporary
  output placeholder.
- Fonts already attached to the source file under the same name are not
  attached a second time; a warning names them.
- Merging runs one file at a time.

## Tests

```sh
python3 -m unittest discover -s tests
```

The unit tests need nothing but Python. The integration test builds a tiny
video, audio file and subtitle with `ffmpeg` and runs the whole flow through
`mkvmerge`; it is skipped when either tool is missing.

## License

MIT. See [LICENSE](LICENSE).
