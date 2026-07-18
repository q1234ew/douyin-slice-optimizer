from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dso.media.video_download import (
    VideoDownloadPolicyError,
    VideoDownloadUnavailableError,
    _load_videodl_tencent_client,
    _select_youtube_streams,
    _validate_url,
    download_video_resource,
)


TEST_URL = "https://v.qq.com/x/cover/demo/video.html?ptag=test#fragment"
YOUTUBE_URL = (
    "https://www.youtube.com/watch?v=pkl-Lr6gkCo"
    "&list=PLL7LXvkhjsoKjUd0vwQNEyssdbCRV_ihp&index=6#fragment"
)
YOUTUBE_CANONICAL_URL = "https://www.youtube.com/watch?v=pkl-Lr6gkCo"


class _FakeInfo:
    def __init__(
        self,
        *,
        has_drm: bool = False,
        identifier: str = "video-1",
        title: str = "测试节目",
        source: str = "TencentVideoClient",
    ) -> None:
        self.source = source
        self.identifier = identifier
        self.title = title
        self.ext = "mp4"
        self.download_url = f"https://media.example.test/{identifier}.m3u8"
        self.save_path = ""
        self.err_msg = ""
        self.raw_data = {
            "formats": [
                {
                    "url": self.download_url,
                    "has_drm": has_drm,
                    "width": 1280,
                    "height": 720,
                }
            ]
        }

    @property
    def with_valid_download_url(self) -> bool:
        return bool(self.download_url)


def _fake_client_class(
    infos: list[_FakeInfo],
    *,
    skip_files: set[str] | None = None,
    source: str = "TencentVideoClient",
):
    skipped = skip_files or set()

    class FakeTencentClient:
        last_instance = None

        def __init__(self, **kwargs) -> None:
            self.config = kwargs
            self.work_dir = Path(kwargs["work_dir"])
            self.parse_calls: list[tuple[str, dict]] = []
            self.download_calls: list[tuple[list[_FakeInfo], int, dict]] = []
            type(self).last_instance = self

        def parsefromurl(self, url: str, request_overrides: dict | None = None):
            self.parse_calls.append((url, request_overrides or {}))
            return infos

        def download(
            self,
            video_infos: list[_FakeInfo],
            num_threadings: int = 5,
            request_overrides: dict | None = None,
        ):
            self.download_calls.append((video_infos, num_threadings, request_overrides or {}))
            output_dir = self.work_dir / source
            output_dir.mkdir(parents=True, exist_ok=True)
            for info in video_infos:
                if info.identifier in skipped:
                    continue
                target = output_dir / f"{info.identifier}.mp4"
                target.write_bytes(b"fake-clear-media")
                info.save_path = str(target)
            return video_infos

    return FakeTencentClient


class VideoDownloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requires_noncommercial_acknowledgement_before_loading_provider(self) -> None:
        with patch("dso.media.video_download._load_videodl_client") as loader:
            with self.assertRaisesRegex(VideoDownloadPolicyError, "PolyForm-Noncommercial"):
                download_video_resource(TEST_URL, output_dir=self.root)

        loader.assert_not_called()

    def test_rejects_unreviewed_hosts(self) -> None:
        with patch("dso.media.video_download._load_videodl_client") as loader:
            with self.assertRaisesRegex(VideoDownloadPolicyError, "video hosts"):
                download_video_resource(
                    "https://example.com/video",
                    output_dir=self.root,
                    acknowledge_noncommercial=True,
                )

        loader.assert_not_called()

    def test_rejects_an_unreviewed_provider_version(self) -> None:
        modules = [
            SimpleNamespace(__version__="0.9.2"),
            SimpleNamespace(TencentVideoClient=object),
        ]
        with patch("dso.media.video_download.importlib.import_module", side_effect=modules):
            with self.assertRaisesRegex(VideoDownloadUnavailableError, "not the audited version"):
                _load_videodl_tencent_client()

    def test_rejects_selected_drm_format_without_downloading(self) -> None:
        client_class = _fake_client_class([_FakeInfo(has_drm=True)])
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ):
            with self.assertRaisesRegex(VideoDownloadPolicyError, "DRM-protected"):
                download_video_resource(
                    TEST_URL,
                    output_dir=self.root,
                    acknowledge_noncommercial=True,
                )

        self.assertEqual(client_class.last_instance.download_calls, [])

    def test_dry_run_writes_policy_manifest_without_downloading(self) -> None:
        client_class = _fake_client_class([_FakeInfo()])
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ):
            result = download_video_resource(
                TEST_URL,
                output_dir=self.root,
                dry_run=True,
                acknowledge_noncommercial=True,
            )

        self.assertEqual(result["status"], "parsed")
        self.assertEqual(result["contract_version"], "video_download.v1")
        self.assertFalse(result["policy"]["cookies_used"])
        self.assertFalse(result["policy"]["generic_parsers_enabled"])
        self.assertFalse(result["policy"]["third_party_parsers_enabled"])
        self.assertFalse(result["policy"]["playlist_expansion_enabled"])
        self.assertFalse(result["policy"]["drm_allowed"])
        self.assertEqual(result["source_url"], TEST_URL.removesuffix("#fragment"))
        self.assertEqual(client_class.last_instance.download_calls, [])
        manifest_path = Path(result["manifest_path"])
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(json.loads(manifest_path.read_text())["status"], "parsed")

    def test_default_output_uses_current_project_temporary_directory(self) -> None:
        client_class = _fake_client_class([_FakeInfo()])
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ), patch(
            "dso.media.video_download.get_settings",
            return_value=SimpleNamespace(data_dir=self.root / "data"),
        ):
            result = download_video_resource(
                TEST_URL,
                dry_run=True,
                acknowledge_noncommercial=True,
            )

        output_dir = Path(result["output_dir"])
        self.assertEqual(output_dir.parent, self.root / "data" / "tmp" / "video_downloads")
        self.assertTrue(output_dir.name.startswith("download_"))

    def test_download_can_enter_existing_ingest_chain(self) -> None:
        client_class = _fake_client_class([_FakeInfo()])
        ingested = {"id": "video-ingested", "status": "ingested"}
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ), patch("dso.media.video_download.ingest_video", return_value=ingested) as ingest_mock:
            result = download_video_resource(
                TEST_URL,
                account_id="main",
                title="实验节目",
                output_dir=self.root,
                threads=3,
                ingest=True,
                acknowledge_noncommercial=True,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["provider_client"], "TencentVideoClient")
        self.assertEqual(len(result["files"]), 1)
        self.assertGreater(result["files"][0]["size_bytes"], 0)
        self.assertEqual(result["ingested_videos"], [ingested])
        ingest_mock.assert_called_once()
        self.assertEqual(ingest_mock.call_args.kwargs, {"account_id": "main", "title": "实验节目"})
        instance = client_class.last_instance
        self.assertEqual(instance.download_calls[0][1], 3)
        self.assertEqual(instance.config["default_download_cookies"], {})
        self.assertFalse(instance.config["auto_set_proxies"])

    def test_item_limit_prevents_accidental_series_download(self) -> None:
        client_class = _fake_client_class([_FakeInfo(identifier="one"), _FakeInfo(identifier="two")])
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ):
            with self.assertRaisesRegex(VideoDownloadPolicyError, "above max_items=1"):
                download_video_resource(
                    TEST_URL,
                    output_dir=self.root,
                    acknowledge_noncommercial=True,
                )

        self.assertEqual(client_class.last_instance.download_calls, [])

    def test_ingest_title_stays_aligned_when_upstream_omits_a_file(self) -> None:
        infos = [
            _FakeInfo(identifier="missing", title="不应入库"),
            _FakeInfo(identifier="ready", title="正确标题"),
        ]
        client_class = _fake_client_class(infos, skip_files={"missing"})
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ), patch("dso.media.video_download.ingest_video", return_value={"id": "video-ready"}) as ingest_mock:
            result = download_video_resource(
                TEST_URL,
                output_dir=self.root,
                max_items=2,
                acknowledge_noncommercial=True,
            )

        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(ingest_mock.call_args.kwargs["title"], "正确标题")

    def test_youtube_url_is_reduced_to_one_canonical_video(self) -> None:
        info = _FakeInfo(source="YouTubeVideoClient", identifier="pkl-Lr6gkCo")
        client_class = _fake_client_class([info], source="YouTubeVideoClient")
        with patch(
            "dso.media.video_download._load_videodl_client",
            return_value=(client_class, "0.9.1"),
        ) as loader:
            result = download_video_resource(
                YOUTUBE_URL,
                output_dir=self.root,
                dry_run=True,
                acknowledge_noncommercial=True,
            )

        loader.assert_called_once_with("youtube")
        self.assertEqual(result["source_provider"], "youtube")
        self.assertEqual(result["provider_client"], "YouTubeVideoClient")
        self.assertEqual(result["source_url"], YOUTUBE_CANONICAL_URL)
        self.assertEqual(result["policy"]["youtube_max_height"], 720)
        self.assertEqual(client_class.last_instance.parse_calls[0][0], YOUTUBE_CANONICAL_URL)

    def test_youtube_short_url_is_normalized_and_playlist_url_is_rejected(self) -> None:
        normalized, provider = _validate_url("https://youtu.be/pkl-Lr6gkCo?t=25")
        self.assertEqual(provider, "youtube")
        self.assertEqual(normalized, YOUTUBE_CANONICAL_URL)

        with self.assertRaisesRegex(VideoDownloadPolicyError, "playlist"):
            _validate_url(
                "https://www.youtube.com/playlist?list=PLL7LXvkhjsoKjUd0vwQNEyssdbCRV_ihp"
            )

    def test_youtube_stream_selection_prefers_720p_h264_and_default_mp4_audio(self) -> None:
        def stream(**kwargs):
            defaults = {
                "includesvideotrack": False,
                "includesaudiotrack": False,
                "subtype": "mp4",
                "resolution": None,
                "issabr": False,
                "url": "https://googlevideo.example.test/media",
                "video_codec": None,
                "audio_codec": None,
                "fps": None,
                "bitrate": 0,
                "is_drc": False,
                "is_default_audio_track": True,
            }
            defaults.update(kwargs)
            return SimpleNamespace(**defaults)

        video_1080 = stream(
            includesvideotrack=True,
            resolution="1080p",
            video_codec="avc1.640028",
            bitrate=4_000_000,
        )
        video_720_av1 = stream(
            includesvideotrack=True,
            resolution="720p",
            video_codec="av01.0.05M.08",
            bitrate=1_800_000,
        )
        video_720_h264 = stream(
            includesvideotrack=True,
            resolution="720p",
            video_codec="avc1.4d401f",
            bitrate=2_300_000,
        )
        audio_low = stream(includesaudiotrack=True, bitrate=48_000)
        audio_high = stream(includesaudiotrack=True, bitrate=128_000)

        selected_video, selected_audio = _select_youtube_streams(
            [video_1080, video_720_av1, video_720_h264, audio_low, audio_high]
        )

        self.assertIs(selected_video, video_720_h264)
        self.assertIs(selected_audio, audio_high)


if __name__ == "__main__":
    unittest.main()
