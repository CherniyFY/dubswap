#!/usr/bin/env python3
"""Merge an external audio track and subtitles into MKV episodes.

For each video in a folder the tool finds exactly one audio file and one
subtitle file with the same episode number, remuxes them into a new MKV
together with the original tracks and fonts, and turns off the default flag
of the original audio and subtitle tracks so the new ones become the defaults.

Source files are never modified. A merged file appears in the output folder
only after mkvmerge succeeded and the result was verified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

VERSION = "0.1.0"

# First pattern that matches a file name wins. Each pattern needs at least one
# capture group; the captured numbers form the episode key.
DEFAULT_EPISODE_PATTERNS = (
    r"[Ss](\d{1,2})[Ee](\d{1,3})",
    r"(?:^|[^0-9])[Ee][Pp]?(\d{1,4})(?![0-9])",
    r"\s-\s(\d{1,4})(?![0-9])",
)
DEFAULT_AUDIO_GLOBS = ("*.mka",)
DEFAULT_SUBS_GLOBS = ("*.ass", "*.ssa")
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")

# mkvmerge translates its human readable messages, but JSON keys are stable,
# so JSON identification plus a C locale keeps parsing predictable.
MKVMERGE_ENV = dict(os.environ, LC_ALL="C")


class SetupError(Exception):
    """Raised when the run cannot start or a file cannot be inspected."""


@dataclass(frozen=True)
class Job:
    video: Path
    audio: Path
    subs: Optional[Path]
    output: Path


@dataclass(frozen=True)
class Options:
    lang: Optional[str]
    audio_name: Optional[str]
    subs_name: Optional[str]
    demote: bool
    fonts: Tuple[Path, ...]


def episode_key(name: str, patterns: Sequence[str]) -> Optional[Tuple[int, ...]]:
    """Return the episode numbers found in a file name, or None."""
    stem = Path(name).stem
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match and match.groups():
            return tuple(int(group) for group in match.groups())
    return None


def keys_match(left: Sequence[int], right: Sequence[int]) -> bool:
    """Compare two episode keys.

    Equal keys match. Keys of different length also match when the shorter one
    is a suffix of the longer one, so ``S01E03`` matches a plain ``03``.
    """
    if not left or not right:
        return False
    if tuple(left) == tuple(right):
        return True
    shorter, longer = sorted((tuple(left), tuple(right)), key=len)
    return longer[len(longer) - len(shorter):] == shorter


def format_key(key: Sequence[int]) -> str:
    return "-".join(str(part) for part in key)


def find_asset(
    kind: str, key: Sequence[int], candidates: Sequence[Tuple[Path, Tuple[int, ...]]]
) -> Path:
    """Return the single candidate whose episode key matches ``key``."""
    matched = [path for path, other in candidates if keys_match(key, other)]
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise SetupError("no %s file for episode %s" % (kind, format_key(key)))
    names = ", ".join(sorted(path.name for path in matched))
    raise SetupError(
        "%d %s files match episode %s, need exactly one: %s"
        % (len(matched), kind, format_key(key), names)
    )


def collect_assets(
    folder: Path,
    globs: Sequence[str],
    patterns: Sequence[str],
    kind: str,
    warnings: List[str],
) -> List[Tuple[Path, Tuple[int, ...]]]:
    """List files matching ``globs`` together with their episode keys."""
    found: List[Tuple[Path, Tuple[int, ...]]] = []
    seen = set()
    for pattern in globs:
        for path in sorted(folder.glob(pattern)):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            key = episode_key(path.name, patterns)
            if key is None:
                warnings.append("%s: no episode number, ignored as %s" % (path.name, kind))
                continue
            found.append((path, key))
    return found


def identify(mkvmerge: str, path: Path) -> Dict[str, object]:
    """Return mkvmerge's JSON identification for ``path``."""
    result = subprocess.run(
        [mkvmerge, "--identify", "--identification-format", "json", str(path)],
        capture_output=True,
        text=True,
        env=MKVMERGE_ENV,
    )
    # Exit code 1 means "finished with warnings"; the JSON is still complete.
    if result.returncode not in (0, 1):
        message = result.stderr.strip() or result.stdout.strip()
        raise SetupError("mkvmerge --identify failed on %s: %s" % (path.name, message))
    try:
        info = json.loads(result.stdout)
    except ValueError as exc:
        raise SetupError(
            "cannot parse mkvmerge identification for %s: %s" % (path.name, exc)
        ) from exc
    if not isinstance(info, dict):
        raise SetupError("unexpected identification output for %s" % path.name)
    return info


def tracks(info: Dict[str, object]) -> List[Dict[str, object]]:
    found = info.get("tracks")
    return found if isinstance(found, list) else []


def demote_source_defaults(info: Dict[str, object]) -> List[str]:
    """Turn off the default flag of every source audio and subtitle track."""
    arguments: List[str] = []
    for track in tracks(info):
        if track.get("type") in ("audio", "subtitles"):
            arguments += ["--default-track-flag", "%s:0" % track.get("id")]
    return arguments


def font_args(
    fonts: Sequence[Path], info: Dict[str, object], warnings: List[str]
) -> List[str]:
    """Attach fonts, skipping names the source already carries."""
    existing = set()
    for attachment in info.get("attachments") or ():
        name = attachment.get("file_name")
        if name:
            existing.add(name)
    arguments: List[str] = []
    for font in fonts:
        if font.name in existing:
            warnings.append(
                "%s: already attached to the source, not attached again" % font.name
            )
            continue
        arguments += ["--attach-file", str(font)]
    return arguments


def track_options(lang: Optional[str], name: Optional[str]) -> List[str]:
    """Per-file options for the next input: language, name, default flag.

    These apply to track 0 of the following input file, which is why they must
    stay right before it on the command line.
    """
    arguments: List[str] = []
    if lang:
        arguments += ["--language", "0:%s" % lang]
    if name:
        arguments += ["--track-name", "0:%s" % name]
    arguments += ["--default-track-flag", "0:1"]
    return arguments


def build_command(
    mkvmerge: str,
    job: Job,
    partial: Path,
    info: Dict[str, object],
    options: Options,
    warnings: List[str],
) -> List[str]:
    """Full mkvmerge argument list for one job."""
    command = [mkvmerge, "-o", str(partial)]
    command += font_args(options.fonts, info, warnings)
    if options.demote:
        command += demote_source_defaults(info)
    command.append(str(job.video))
    command += track_options(options.lang, options.audio_name)
    command.append(str(job.audio))
    if job.subs is not None:
        command += track_options(options.lang, options.subs_name)
        command.append(str(job.subs))
    return command


def expected_track_count(info: Dict[str, object], with_subs: bool) -> int:
    return len(tracks(info)) + 1 + (1 if with_subs else 0)


def kept_note(partial: Path) -> str:
    """Text for a failure message, mentioning the partial file only if it exists."""
    if partial.is_file():
        return ", %s kept for inspection" % partial
    return ""


def verify_output(mkvmerge: str, path: Path, expected: int) -> Optional[str]:
    """Return a problem description for a finished file, or None."""
    try:
        info = identify(mkvmerge, path)
    except SetupError as exc:
        return str(exc)
    count = len(tracks(info))
    if count != expected:
        return "output has %d tracks, expected %d" % (count, expected)
    return None


def merge_job(
    mkvmerge: str,
    job: Job,
    info: Dict[str, object],
    options: Options,
    dry_run: bool,
    force: bool,
) -> Tuple[int, int, int]:
    """Return counts of created, skipped and failed files for one job."""
    # Keep an unfinished merge off the final path, and away from other runs.
    staging = job.output.parent / ".dubswap-TEMP"
    if not dry_run:
        try:
            staging = Path(tempfile.mkdtemp(prefix=".dubswap-", dir=str(job.output.parent)))
        except OSError as exc:
            print("FAILED %s: %s" % (job.video.name, exc), file=sys.stderr)
            return 0, 0, 1
    partial = staging / "output.part"
    try:
        warnings: List[str] = []
        command = build_command(mkvmerge, job, partial, info, options, warnings)
        for warning in warnings:
            print("warning: %s: %s" % (job.video.name, warning), file=sys.stderr)
        if dry_run:
            print(shlex.join(command))
            return 0, 0, 0

        result = subprocess.run(command, env=MKVMERGE_ENV)
        if result.returncode not in (0, 1):
            raise SetupError("mkvmerge exit code %d" % result.returncode)
        problem = verify_output(mkvmerge, partial, expected_track_count(info, job.subs is not None))
        if problem:
            raise SetupError(problem)

        if force:
            os.replace(partial, job.output)
        else:
            # Linking the verified file publishes it without replacing a concurrent result.
            try:
                os.link(partial, job.output)
            except FileExistsError:
                print("skipped %s: %s exists" % (job.video.name, job.output.name))
                partial.unlink()
                return 0, 1, 0
            partial.unlink()
        print("created %s" % job.output.name)
        return 1, 0, 0
    except (OSError, SetupError) as exc:
        print("FAILED %s: %s%s" % (job.video.name, exc, kept_note(partial)), file=sys.stderr)
        return 0, 0, 1
    except KeyboardInterrupt:
        note = kept_note(partial)
        if note:
            print("%s%s" % (job.video.name, note), file=sys.stderr)
        raise
    finally:
        if not dry_run and not partial.exists():
            staging.rmdir()


def find_mkvmerge(explicit: Optional[str]) -> str:
    if explicit:
        found = shutil.which(explicit)
        if found:
            return found
        candidate = Path(explicit).expanduser()
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
        raise SetupError("no executable mkvmerge at %s" % explicit)
    found = shutil.which("mkvmerge")
    if found:
        return found
    app = Path("/Applications/MKVToolNix.app/Contents/MacOS/mkvmerge")
    if app.is_file():
        return str(app)
    raise SetupError("mkvmerge not found; install MKVToolNix or pass --mkvmerge PATH")


def resolve(folder: Path, value: str) -> Path:
    """Resolve a command line path, interpreting relative ones below ``folder``."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else folder / path


def file_identity(path: Path) -> Tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def font_files(folder: Path, extra: Sequence[str], warnings: List[str]) -> Tuple[Path, ...]:
    """Fonts from every given folder, or from FOLDER/Fonts by default."""
    directories = [resolve(folder, value) for value in extra] or [folder / "Fonts"]
    fonts: List[Path] = []
    for directory in directories:
        if not directory.is_dir():
            warnings.append("no font folder at %s" % directory)
            continue
        for path in sorted(directory.iterdir()):
            if path.is_file() and path.suffix.lower() in FONT_SUFFIXES:
                fonts.append(path)
    return tuple(fonts)


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dubswap",
        description="Merge an external audio track and subtitles into MKV episodes.",
        epilog=(
            "Episode numbers are read from file names: S01E03, EP03 and ' - 03' are "
            "understood by default; use --episode-regex for other naming."
        ),
    )
    parser.add_argument("folder", metavar="FOLDER", help="folder with the source .mkv files")
    parser.add_argument(
        "--audio",
        action="append",
        metavar="GLOB",
        help="audio file names to look for, repeatable (default: %s)"
        % " ".join(DEFAULT_AUDIO_GLOBS),
    )
    parser.add_argument(
        "--subs",
        action="append",
        metavar="GLOB",
        help="subtitle file names to look for, repeatable (default: %s)"
        % " ".join(DEFAULT_SUBS_GLOBS),
    )
    parser.add_argument("--no-subs", action="store_true", help="merge audio only")
    parser.add_argument(
        "--audio-dir", metavar="DIR", help="where the audio files are, below FOLDER (default: FOLDER)"
    )
    parser.add_argument(
        "--subs-dir", metavar="DIR", help="where the subtitles are, below FOLDER (default: FOLDER)"
    )
    parser.add_argument(
        "--fonts",
        action="append",
        metavar="DIR",
        help="folder with subtitle fonts, repeatable (default: FOLDER/Fonts)",
    )
    parser.add_argument("--lang", metavar="CODE", help="language code for the new tracks, e.g. eng")
    parser.add_argument("--audio-name", metavar="NAME", help="track name for the new audio")
    parser.add_argument("--subs-name", metavar="NAME", help="track name for the new subtitles")
    parser.add_argument(
        "--episode-regex",
        action="append",
        metavar="REGEX",
        help="episode number regex with capture groups, repeatable, first match wins",
    )
    parser.add_argument(
        "--out", metavar="DIR", help="output folder, below FOLDER (default: FOLDER/merged)"
    )
    parser.add_argument(
        "--suffix",
        default=".merged",
        metavar="TEXT",
        help="added to output file names (default: .merged)",
    )
    parser.add_argument(
        "--keep-original-defaults",
        dest="demote",
        action="store_false",
        help="leave the default flag on the original audio and subtitle tracks",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the mkvmerge commands, write nothing"
    )
    parser.add_argument("--mkvmerge", metavar="PATH", help="use this mkvmerge binary")
    parser.add_argument("--version", action="version", version="dubswap " + VERSION)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    source = Path(args.folder).expanduser().resolve()
    if not source.is_dir():
        raise SetupError("%s is not a folder" % source)

    patterns = tuple(args.episode_regex or DEFAULT_EPISODE_PATTERNS)
    for pattern in patterns:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise SetupError("bad --episode-regex %r: %s" % (pattern, exc)) from exc
        if compiled.groups < 1:
            raise SetupError("--episode-regex %r needs at least one capture group" % pattern)

    if any(separator in args.suffix for separator in (os.sep, os.altsep) if separator):
        raise SetupError("--suffix must not contain a path separator")

    mkvmerge = find_mkvmerge(args.mkvmerge)
    out_dir = (resolve(source, args.out) if args.out else source / "merged").resolve()
    audio_dir = resolve(source, args.audio_dir) if args.audio_dir else source
    subs_dir = resolve(source, args.subs_dir) if args.subs_dir else source
    for kind, directory in (("audio", audio_dir), ("subtitle", subs_dir)):
        if not directory.is_dir():
            raise SetupError("%s folder not found: %s" % (kind, directory))

    videos = sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() == ".mkv")
    if not videos:
        raise SetupError("no .mkv files in %s" % source)

    warnings: List[str] = []
    fonts = font_files(source, args.fonts or (), warnings)
    if not fonts:
        warnings.append("no fonts found; signs may render with fallback fonts")
    audio_globs = tuple(args.audio or DEFAULT_AUDIO_GLOBS)
    subs_globs = tuple(args.subs or DEFAULT_SUBS_GLOBS)
    audio_candidates = collect_assets(audio_dir, audio_globs, patterns, "audio", warnings)
    subs_candidates = (
        []
        if args.no_subs
        else collect_assets(subs_dir, subs_globs, patterns, "subtitle", warnings)
    )
    input_paths = videos + [path for path, _ in audio_candidates + subs_candidates] + list(fonts)
    input_files = {file_identity(path) for path in input_paths}
    outputs = {video: out_dir / (video.stem + args.suffix + ".mkv") for video in videos}
    if len(set(outputs.values())) != len(outputs):
        raise SetupError("multiple source videos would produce the same output file name")
    for output in outputs.values():
        try:
            identity = file_identity(output)
        except FileNotFoundError:
            continue
        if identity in input_files:
            raise SetupError("output would replace an input file: %s" % output)
    for message in warnings:
        print("warning: %s" % message, file=sys.stderr)

    options = Options(
        lang=args.lang,
        audio_name=args.audio_name,
        subs_name=args.subs_name,
        demote=args.demote,
        fonts=fonts,
    )
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    created = skipped = unmatched = failed = 0
    for video in videos:
        key = episode_key(video.name, patterns)
        if key is None:
            print("skipped %s: no episode number" % video.name, file=sys.stderr)
            unmatched += 1
            continue
        try:
            audio = find_asset("audio", key, audio_candidates)
            subs = None if args.no_subs else find_asset("subtitle", key, subs_candidates)
        except SetupError as exc:
            print("skipped %s: %s" % (video.name, exc), file=sys.stderr)
            unmatched += 1
            continue

        job = Job(video=video, audio=audio, subs=subs, output=outputs[video])
        if not args.force and (job.output.exists() or job.output.is_symlink()):
            print("skipped %s: %s exists" % (video.name, job.output.name))
            skipped += 1
            continue
        try:
            info = identify(mkvmerge, video)
        except SetupError as exc:
            print("FAILED %s: %s" % (video.name, exc), file=sys.stderr)
            failed += 1
            continue

        print(
            "episode %s: %s%s"
            % (format_key(key), video.name, "" if subs is None else " + " + subs.name)
        )
        job_created, job_skipped, job_failed = merge_job(
            mkvmerge, job, info, options, args.dry_run, args.force
        )
        created += job_created
        skipped += job_skipped
        failed += job_failed

    if args.dry_run:
        print("dry run, nothing was written")
    print(
        "%d created, %d already present, %d without a matching file, %d failed"
        % (created, skipped, unmatched, failed)
    )
    return 1 if (failed or unmatched) else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (OSError, SetupError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
