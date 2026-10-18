from creator_report.sources import detect_platform, normalize_source_url

# All source URLs in this module are synthetic and are never fetched.


def test_detect_platform_recognizes_youtube_channel_url() -> None:
    platform = detect_platform("https://www.youtube.com/@synthetic-channel/videos")
    assert platform == "youtube"


def test_detect_platform_recognizes_bilibili_space_url() -> None:
    platform = detect_platform("https://space.bilibili.com/synthetic-channel")
    assert platform == "bilibili"


def test_normalize_source_url_strips_query_parameters() -> None:
    normalized = normalize_source_url("https://www.youtube.com/@synthetic-channel/videos?view=0")
    assert normalized == "https://www.youtube.com/@synthetic-channel/videos"


def test_normalize_source_url_accepts_bilibili_tracking_suffix() -> None:
    normalized = normalize_source_url(
        "https://space.bilibili.com/synthetic-channel/?spm_id_from=synthetic.tracking"
    )

    assert normalized == "https://space.bilibili.com/synthetic-channel"
