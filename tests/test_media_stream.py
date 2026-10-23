import httpx
import pytest

from bili_platform.media_stream import open_media
from bili_platform.protocol import PlatformError


def setup_upstream(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr("bili_platform.media_stream.httpx.Client",
                        lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    return original(trust_env=False, timeout=5)


def test_range_proxy_forwards_only_range_and_never_saves_source(monkeypatch, tmp_path):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(206, content=b"frame", headers={
            "Content-Range": "bytes 10-14/100", "Set-Cookie": "secret=bad"})
    local = setup_upstream(monkeypatch, handler)
    with local, open_media("https://x.bilivideo.com/a") as media:
        for _ in range(2):
            response = local.get(media.url, headers={
                "Range": "bytes=10-14", "Cookie": "private=yes", "Authorization": "secret"})
            assert response.status_code == 206 and response.content == b"frame"
        assert media.bytes_read == 10 and media.requests == 2 and media.error is None
        assert len(seen) == 2
        assert all(r.headers["range"] == "bytes=10-14" for r in seen)
        assert all("cookie" not in r.headers and "authorization" not in r.headers for r in seen)
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("status", [302, 403])
def test_upstream_rejection_does_not_follow_redirects(monkeypatch, status):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"Location": "https://evil.test/"})
    local = setup_upstream(monkeypatch, handler)
    with local, open_media("https://x.bilivideo.com/a") as media:
        assert local.get(media.url).status_code == 502
        assert media.error.kind == "transient" and len(seen) == 1


def test_proxy_is_not_a_general_local_forwarder(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(206, content=b"x")
    local = setup_upstream(monkeypatch, handler)
    with local, open_media("https://x.bilivideo.com/a") as media:
        assert local.get(media.url + "wrong").status_code == 403
        assert local.get(media.url, headers={"Host": "evil.test"}).status_code == 403
        assert local.get(media.url, headers={"Range": "bytes=0-1,3-5"}).status_code == 400
        assert seen == []


def test_transfer_budget_stops_future_requests(monkeypatch):
    local = setup_upstream(monkeypatch, lambda r: httpx.Response(206, content=b"x" * 201))
    with local, open_media("https://x.bilivideo.com/a", max_bytes=200) as media:
        with pytest.raises(httpx.RemoteProtocolError):
            local.get(media.url)
        assert media.error.kind == "budget"
        assert local.get(media.url).status_code == 502
        assert media.requests == 1


def test_cancellation_does_not_contact_upstream(monkeypatch):
    def cancelled():
        raise PlatformError("cancelled", "停止")
    seen = []
    local = setup_upstream(monkeypatch, lambda r: seen.append(r))
    with local, open_media("https://x.bilivideo.com/a", check=cancelled) as media:
        assert local.get(media.url).status_code == 502
        assert media.error.kind == "cancelled" and not seen
