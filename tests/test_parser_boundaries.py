from threads_parser.parser import extract_posts, merge_posts


def test_profile_extraction_deduplicates_posts_and_keyword_filtering_stays_casefolded() -> None:
    post = {
        "id": "post-001",
        "user": {"username": "sample_user"},
        "caption": {"text": "New MACHINE learning model"},
        "taken_at": 1_700_000_000,
        "image_versions2": {"candidates": [{"url": "https://cdn.example/post.jpg", "width": 640, "height": 480}]},
    }

    posts = extract_posts([{"items": [post, post]}], "sample_user")

    assert len(posts) == 1
    assert posts[0]["id"] == "post-001"
    assert posts[0]["text"] == "New MACHINE learning model"
    assert posts[0]["media"][0]["url"] == "https://cdn.example/post.jpg"


def test_incremental_merge_retains_local_media_and_updates_post_text() -> None:
    saved = [{
        "id": "post-001",
        "username": "sample_user",
        "text": "Earlier text",
        "permalink": "https://www.threads.com/@sample_user/post/post-001",
        "media": [{"type": "image", "url": "https://cdn.example/post.jpg", "local_path": "output/sample_user/media/post.jpg"}],
    }]
    incoming = [{
        "id": "post-001",
        "username": "sample_user",
        "text": "Updated post text",
        "permalink": "https://www.threads.com/@sample_user/post/post-001",
        "media": [{"type": "image", "url": "https://cdn.example/post.jpg", "local_path": ""}],
    }]

    merged = merge_posts(saved, incoming)

    assert len(merged) == 1
    assert merged[0]["text"] == "Updated post text"
    assert merged[0]["media"][0]["local_path"] == "output/sample_user/media/post.jpg"
