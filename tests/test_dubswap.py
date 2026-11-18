#!/usr/bin/env python3
"""Tests for dubswap. Run with: python3 -m unittest discover -s tests"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dubswap


def sample_info():
    return {
        "container": {"type": "Matroska"},
        "tracks": [
            {"id": 0, "type": "video"},
            {"id": 1, "type": "audio"},
            {"id": 2, "type": "audio"},
            {"id": 3, "type": "subtitles"},
        ],
        "attachments": [{"id": 1, "file_name": "existing.otf"}],
    }


class EpisodeKeyTests(unittest.TestCase):
    def key(self, name, patterns=None):
        return dubswap.episode_key(name, patterns or dubswap.DEFAULT_EPISODE_PATTERNS)

    def test_season_episode(self):
        self.assertEqual(self.key("Show S02E07 [1080p].mkv"), (2, 7))

    def test_episode_prefix(self):
        self.assertEqual(self.key("Show EP07.mka"), (7,))
        self.assertEqual(self.key("Show.E07.mka"), (7,))

    def test_dash_separated(self):
        self.assertEqual(self.key("Show - 07 [1080p][ABCD].mkv"), (7,))

    def test_no_number(self):
        self.assertIsNone(self.key("Show - Movie.mkv"))

    def test_custom_pattern(self):
        self.assertEqual(self.key("Show_07.mkv", (r"_(\d{2})$",)), (7,))

    def test_first_matching_pattern_wins(self):
        self.assertEqual(self.key("Show S01E07.mkv", (r"S(\d{2})", r"(\d+)")), (1,))

    def test_leading_zeros_are_numbers(self):
        self.assertEqual(self.key("Show - 007.mkv"), (7,))


class KeysMatchTests(unittest.TestCase):
    def test_equal(self):
        self.assertTrue(dubswap.keys_match((1, 3), (1, 3)))

    def test_shorter_suffix_matches(self):
        self.assertTrue(dubswap.keys_match((1, 3), (3,)))
        self.assertTrue(dubswap.keys_match((3,), (1, 3)))

    def test_different(self):
        self.assertFalse(dubswap.keys_match((3,), (4,)))
        self.assertFalse(dubswap.keys_match((1, 2), (1, 3)))
        self.assertFalse(dubswap.keys_match((12,), (2,)))

    def test_empty(self):
        self.assertFalse(dubswap.keys_match((), ()))


class FindAssetTests(unittest.TestCase):
    def test_single_match(self):
        candidates = [(Path("a - 01.mka"), (1,)), (Path("a - 02.mka"), (2,))]
        self.assertEqual(dubswap.find_asset("audio", (2,), candidates), Path("a - 02.mka"))

    def test_no_match(self):
        with self.assertRaises(dubswap.SetupError) as caught:
            dubswap.find_asset("audio", (5,), [(Path("a - 01.mka"), (1,))])
        self.assertIn("no audio file for episode 5", str(caught.exception))

    def test_ambiguous_match(self):
        candidates = [(Path("a - 01.mka"), (1,)), (Path("b - 01.mka"), (1,))]
        with self.assertRaises(dubswap.SetupError) as caught:
            dubswap.find_asset("audio", (1,), candidates)
        self.assertIn("2 audio files match episode 1", str(caught.exception))


class CollectAssetsTests(unittest.TestCase):
    def test_keys_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a - 01.mka").write_bytes(b"")
            (folder / "notes.mka").write_bytes(b"")
            warnings = []
            found = dubswap.collect_assets(
                folder, ("*.mka",), dubswap.DEFAULT_EPISODE_PATTERNS, "audio", warnings
            )
            self.assertEqual([key for _, key in found], [(1,)])
            self.assertEqual(len(warnings), 1)
            self.assertIn("notes.mka", warnings[0])

    def test_overlapping_globs_list_a_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a - 01.eac3").write_bytes(b"")
            found = dubswap.collect_assets(
                folder, ("*.eac3", "a*"), dubswap.DEFAULT_EPISODE_PATTERNS, "audio", []
            )
            self.assertEqual(len(found), 1)


class DemoteTests(unittest.TestCase):
    def test_audio_and_subtitles_are_demoted(self):
        self.assertEqual(
            dubswap.demote_source_defaults(sample_info()),
            [
                "--default-track-flag", "1:0",
                "--default-track-flag", "2:0",
                "--default-track-flag", "3:0",
            ],
        )


class FontArgsTests(unittest.TestCase):
    def test_attachment_name_collision_is_skipped(self):
        warnings = []
        arguments = dubswap.font_args(
            [Path("existing.otf"), Path("new.ttf")], sample_info(), warnings
        )
        self.assertEqual(arguments, ["--attach-file", "new.ttf"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("existing.otf", warnings[0])


class BuildCommandTests(unittest.TestCase):
    def job(self, subs=True):
        return dubswap.Job(
            video=Path("/src/Show S01E03.mkv"),
            audio=Path("/src/dub/Show - 03.mka"),
            subs=Path("/src/subs/Show - 03.ass") if subs else None,
            output=Path("/src/merged/Show S01E03.merged.mkv"),
        )

    def options(self, demote=True):
        return dubswap.Options(
            lang="eng",
            audio_name="Dub",
            subs_name="Signs",
            demote=demote,
            fonts=(Path("/src/Fonts/a.ttf"),),
        )

    def build(self, subs=True, demote=True):
        return dubswap.build_command(
            "mkvmerge", self.job(subs), Path("/src/merged/.dubswap-temp/output.part"),
            sample_info(), self.options(demote), [],
        )

    def test_output_goes_to_part_file(self):
        self.assertEqual(
            self.build()[:3],
            ["mkvmerge", "-o", "/src/merged/.dubswap-temp/output.part"],
        )

    def test_per_file_options_precede_their_input(self):
        command = self.build()
        audio = command.index("/src/dub/Show - 03.mka")
        self.assertEqual(
            command[audio - 6:audio],
            ["--language", "0:eng", "--track-name", "0:Dub", "--default-track-flag", "0:1"],
        )
        subs = command.index("/src/subs/Show - 03.ass")
        self.assertEqual(
            command[subs - 6:subs],
            ["--language", "0:eng", "--track-name", "0:Signs", "--default-track-flag", "0:1"],
        )

    def test_source_file_comes_before_the_new_tracks(self):
        command = self.build()
        self.assertLess(command.index("/src/Show S01E03.mkv"), command.index("/src/dub/Show - 03.mka"))

    def test_fonts_are_attached(self):
        command = self.build()
        self.assertEqual(command[command.index("--attach-file") + 1], "/src/Fonts/a.ttf")

    def test_original_defaults_kept_on_request(self):
        self.assertNotIn("1:0", self.build(demote=False))

    def test_audio_only(self):
        command = self.build(subs=False)
        self.assertNotIn("/src/subs/Show - 03.ass", command)
        self.assertEqual(dubswap.expected_track_count(sample_info(), False), 5)
        self.assertEqual(dubswap.expected_track_count(sample_info(), True), 6)


class RunTests(unittest.TestCase):
    def test_missing_folder(self):
        self.assertEqual(dubswap.main(["/nonexistent/dubswap"]), 2)

    def test_regex_without_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.mkv").write_bytes(b"")
            self.assertEqual(dubswap.main([tmp, "--episode-regex", r"\d+"]), 2)

    def test_folder_without_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(dubswap, "find_mkvmerge", return_value="mkvmerge"):
                self.assertEqual(dubswap.main([tmp]), 2)

    def test_force_cannot_replace_source_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Show - 01.mkv"
            source.write_bytes(b"original video")
            with patch.object(dubswap, "find_mkvmerge", return_value="mkvmerge"), \
                    patch.object(dubswap, "identify") as identify:
                self.assertEqual(dubswap.main([tmp, "--out", ".", "--suffix", "", "--force"]), 2)
                identify.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original video")

    def test_force_cannot_replace_audio_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "audio").mkdir()
            (folder / "Show - 01.mkv").touch()
            audio = folder / "audio" / "Show - 01.merged.mkv"
            audio.write_bytes(b"original audio")
            arguments = [tmp, "--audio-dir", "audio", "--audio", "*.mkv", "--out", "audio", "--force"]
            with patch.object(dubswap, "find_mkvmerge", return_value="mkvmerge"), \
                    patch.object(dubswap, "identify") as identify:
                self.assertEqual(dubswap.main(arguments), 2)
                identify.assert_not_called()
            self.assertEqual(audio.read_bytes(), b"original audio")

    def test_force_cannot_replace_hard_link_to_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "merged").mkdir()
            source = folder / "Show - 01.mkv"
            source.write_bytes(b"original video")
            output = folder / "merged" / "Show - 01.merged.mkv"
            os.link(source, output)
            with patch.object(dubswap, "find_mkvmerge", return_value="mkvmerge"):
                self.assertEqual(dubswap.main([tmp, "--force"]), 2)
            self.assertTrue(source.samefile(output))

    def test_suffix_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dubswap.main([tmp, "--suffix", "/../another"]), 2)


class MergeJobTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.job = dubswap.Job(folder / "show.mkv", folder / "dub.mka", None, folder / "out.mkv")
        self.options = dubswap.Options(None, None, None, True, ())

    def test_terminated_mux_is_never_published(self):
        def terminated(command, **kwargs):
            Path(command[2]).write_bytes(b"partial merge")
            return subprocess.CompletedProcess(command, -15)

        with patch.object(dubswap.subprocess, "run", side_effect=terminated), \
                patch.object(dubswap, "verify_output") as verify:
            self.assertEqual(
                dubswap.merge_job("mkvmerge", self.job, sample_info(), self.options, False, False),
                (0, 0, 1),
            )
            verify.assert_not_called()
        self.assertFalse(self.job.output.exists())
        partial, = self.job.output.parent.glob(".dubswap-*/output.part")
        self.assertEqual(partial.read_bytes(), b"partial merge")

    def test_no_force_preserves_output_created_during_mux(self):
        def completed(command, **kwargs):
            Path(command[2]).write_bytes(b"verified staged output")
            self.job.output.write_bytes(b"other run's output")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(dubswap.subprocess, "run", side_effect=completed), \
                patch.object(dubswap, "verify_output", return_value=None):
            self.assertEqual(
                dubswap.merge_job("mkvmerge", self.job, sample_info(), self.options, False, False),
                (0, 1, 0),
            )
        self.assertEqual(self.job.output.read_bytes(), b"other run's output")
        self.assertEqual(list(self.job.output.parent.glob(".dubswap-*")), [])

    def test_merge_uses_private_staging_and_leaves_existing_partial_alias(self):
        old_partial = self.job.output.with_name(self.job.output.name + ".part")
        target = self.job.output.parent / "unrelated.txt"
        try:
            old_partial.symlink_to(target)
        except OSError:
            self.skipTest("symbolic links are unavailable")

        def completed(command, **kwargs):
            Path(command[2]).write_bytes(b"verified output")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(dubswap.subprocess, "run", side_effect=completed), \
                patch.object(dubswap, "verify_output", return_value=None):
            self.assertEqual(
                dubswap.merge_job("mkvmerge", self.job, sample_info(), self.options, False, False),
                (1, 0, 0),
            )
        self.assertEqual(self.job.output.read_bytes(), b"verified output")
        self.assertTrue(old_partial.is_symlink())
        self.assertFalse(target.exists())
        self.assertEqual(list(self.job.output.parent.glob(".dubswap-*")), [])


class PathTests(unittest.TestCase):
    def test_relative_paths_resolve_below_the_source_folder(self):
        self.assertEqual(dubswap.resolve(Path("/src"), "dub"), Path("/src/dub"))
        self.assertEqual(dubswap.resolve(Path("/src"), "/abs"), Path("/abs"))

    def test_font_files_default_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "Fonts").mkdir()
            (folder / "Fonts" / "a.ttf").write_bytes(b"")
            (folder / "Fonts" / "readme.txt").write_bytes(b"")
            warnings = []
            self.assertEqual(
                dubswap.font_files(folder, (), warnings), (folder / "Fonts" / "a.ttf",)
            )
            self.assertEqual(warnings, [])

    def test_missing_explicit_font_folder_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            warnings = []
            self.assertEqual(dubswap.font_files(Path(tmp), ("nope",), warnings), ())
            self.assertEqual(len(warnings), 1)


def have_tools():
    return bool(shutil.which("mkvmerge") and shutil.which("ffmpeg"))


@unittest.skipUnless(have_tools(), "mkvmerge and ffmpeg are needed")
class IntegrationTests(unittest.TestCase):
    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            self.build_sample(source)
            arguments = [
                str(source), "--lang", "eng", "--audio-name", "Dub", "--subs-name", "Signs",
            ]
            self.assertEqual(dubswap.main(arguments), 0)

            merged = source / "merged" / "Show - 01 [1080p].merged.mkv"
            self.assertTrue(merged.is_file())
            self.assertEqual(list(merged.parent.glob(".dubswap-*")), [])

            mkvmerge = shutil.which("mkvmerge")
            info = dubswap.identify(mkvmerge, merged)
            self.assertEqual(len(info["tracks"]), 4)
            self.assertEqual([item["file_name"] for item in info["attachments"]], ["a.ttf"])
            audio = [track["properties"] for track in info["tracks"] if track["type"] == "audio"]
            self.assertEqual([track["default_track"] for track in audio], [False, True])
            self.assertEqual(audio[1]["track_name"], "Dub")
            subtitles = next(track["properties"] for track in info["tracks"] if track["type"] == "subtitles")
            self.assertTrue(subtitles["default_track"])
            self.assertEqual(subtitles["track_name"], "Signs")

            # A second run leaves everything alone.
            self.assertEqual(dubswap.main([str(source)]), 0)

    def build_sample(self, source):
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "mpeg4", "-c:a", "aac", "-shortest",
                str(source / "Show - 01 [1080p].mkv"),
            ],
            check=True,
        )
        subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                "-c:a", "aac", str(source / "Dub - 01.mka"),
            ],
            check=True,
        )
        (source / "Signs - 01.ass").write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,hello\n",
            encoding="utf-8",
        )
        (source / "Fonts").mkdir()
        (source / "Fonts" / "a.ttf").write_bytes(b"placeholder font data")


if __name__ == "__main__":
    unittest.main()
