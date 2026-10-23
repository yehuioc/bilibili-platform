import httpx
import pytest

from bili_platform import BilibiliClient, PlatformError
from bili_platform.protocol import json_response, sign_wbi, subtitle_url


@pytest.mark.parametrize(
    "code,kind",
    [(401, "auth"), (403, "auth"), (412, "rate_limit"), (429, "rate_limit"), (503, "transient")],
)
def test_http_failures_are_not_empty_results(code, kind):
    with pytest.raises(PlatformError) as caught:
        json_response(httpx.Response(code))
    assert caught.value.kind == kind


@pytest.mark.parametrize("payload", [[], None, "bad", {"code": False}, {"data": []}])
def test_response_shape_changes_are_visible(payload):
    with pytest.raises(PlatformError):
        json_response(httpx.Response(200, text=__import__("json").dumps(payload)))


@pytest.mark.parametrize(
    "url",
    [
        "http://aisubtitle.hdslb.com/a",
        "https://hdslb.com.evil/a",
        "https://evil.test/a",
        "https://user:pass@hdslb.com/a",
        "https://hdslb.com:123/a",
    ],
)
def test_subtitle_url_rejects_untrusted_destinations(url):
    with pytest.raises(PlatformError):
        subtitle_url(url, 1, 2)


def test_subtitle_identity_and_signature():
    assert subtitle_url(
        "https://aisubtitle.hdslb.com/prod/11706970204499040772045707.json",
        117069702044990,
        40772045707,
    )
    with pytest.raises(PlatformError):
        subtitle_url("https://aisubtitle.hdslb.com/a?cid=99", 1, 2)
    signed = sign_wbi({"keyword": "a!b'c(d)*e"}, "x" * 32, 123)
    assert signed["keyword"] == "abcde" and signed["wts"] == "123"
    assert len(signed["w_rid"]) == 32


def test_transcript_uses_cookie_free_transport_and_keeps_timing():
    calls = []

    def api(request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "cid": 2,
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "ai-zh",
                                "subtitle_url": "https://aisubtitle.hdslb.com/a?aid=1&cid=2",
                            }
                        ]
                    },
                },
            },
        )

    def public(request):
        calls.append(request)
        return httpx.Response(200, json={"body": [{"from": 1.2, "to": 2.4, "content": "原文"}]})

    c = BilibiliClient(
        http=httpx.Client(transport=httpx.MockTransport(api)),
        public_http=httpx.Client(transport=httpx.MockTransport(public)),
        pace=lambda: None,
    )
    result = c.transcript("BVfixture", 2, 1)
    assert result["source"] == "ai_subtitle"
    assert result["segments"][0] == {"start": 1.2, "end": 2.4, "text": "原文"}
    assert "cookie" not in calls[0].headers


def test_redirect_is_not_followed():
    calls = []

    def api(request):
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.test/"})

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    with pytest.raises(PlatformError):
        c.nav()
    assert calls == ["api.bilibili.com"]
    c.close()


def test_repeated_pagination_does_not_claim_complete():
    def api(request):
        return httpx.Response(
            200, json={"code": 0, "data": {"has_more": True, "medias": [{"id": 1}]}}
        )

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    with pytest.raises(PlatformError) as caught:
        c.inventory({"id": 9})
    assert caught.value.kind == "pagination"
    c.close()


def test_write_timeout_is_not_retried():
    calls = []

    def api(request):
        calls.append(request)
        raise httpx.ReadTimeout("fixture")

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    c.csrf = "fixture"
    with pytest.raises(PlatformError) as caught:
        c.create_folder("鸡汤")
    assert caught.value.kind == "write_uncertain" and len(calls) == 1
    c.close()


def test_unbound_player_subtitles_fall_back_to_bound_wbi():
    fetched = []

    def api(request):
        url = "https://aisubtitle.hdslb.com/unrelated.json"
        if "/wbi/" in request.url.path:
            url = "https://aisubtitle.hdslb.com/1/2.json"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"subtitle": {"subtitles": [{"lan": "zh", "subtitle_url": url}]}},
            },
        )

    def public(request):
        fetched.append(request.url.path)
        return httpx.Response(200, json={"body": [{"content": "正确内容"}]})

    c = BilibiliClient(
        http=httpx.Client(transport=httpx.MockTransport(api)),
        public_http=httpx.Client(transport=httpx.MockTransport(public)),
        pace=lambda: None,
    )
    c.key = "fixture"
    value = c.transcript("BVfixture", 2, 1)
    assert value["identity_verified"] and fetched == ["/1/2.json"]


def test_unbound_tracks_never_download_or_become_no_subtitle():
    def api(request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [{"subtitle_url": "https://aisubtitle.hdslb.com/other.json"}]
                    }
                },
            },
        )

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    c.key = "fixture"
    with pytest.raises(PlatformError) as caught:
        c.transcript("BVfixture", 2, 1)
    assert caught.value.kind == "identity"
    c.close()


@pytest.mark.parametrize(
    "data,kind",
    [
        ({"aid": 99, "cid": 2}, "identity"),
        ({"aid": 1, "cid": 99}, "identity"),
        ({"subtitle": None}, "transient"),
        ({"subtitle": {"subtitles": {}}}, "transient"),
    ],
)
def test_player_identity_and_nested_shape_fail_closed(data, kind):
    def api(request):
        return httpx.Response(200, json={"code": 0, "data": data})

    client = BilibiliClient(
        http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None
    )
    with pytest.raises(PlatformError) as caught:
        client.transcript("BVfixture", 2, 1)
    assert caught.value.kind == kind


def test_changing_ai_subtitle_body_is_not_verified():
    calls = []

    def api(request):
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "ai-zh",
                                "subtitle_url": "https://aisubtitle.hdslb.com/1/2.json",
                            }
                        ]
                    }
                },
            },
        )

    def public(request):
        calls.append(request)
        return httpx.Response(200, json={"body": [{"content": str(len(calls))}]})

    client = BilibiliClient(
        http=httpx.Client(transport=httpx.MockTransport(api)),
        public_http=httpx.Client(transport=httpx.MockTransport(public)),
        pace=lambda: None,
    )
    with pytest.raises(PlatformError) as caught:
        client.transcript("BVfixture", 2, 1)
    assert caught.value.kind == "identity" and len(calls) == 2


@pytest.mark.parametrize('identity', [
    {'bvid':'BVfixture','aid':1,'cid':2},
    {'bvid':'BVwrong','aid':1,'cid':2},
    {'bvid':'BVfixture','aid':99,'cid':2},
    {'bvid':'BVfixture','aid':1,'cid':99},
    {'aid':1,'cid':2},
    {'bvid':'BVfixture','cid':2},
])
def test_even_matching_player_identity_cannot_authorize_opaque_subtitles(identity):
    fetched=[]
    def api(request):
        return httpx.Response(200,json={'code':0,'data':{**identity,'subtitle':{'subtitles':[
            {'lan':'ai-zh','subtitle_url':'https://aisubtitle.hdslb.com/bfs/opaque-hash'}]}}})
    def public(request):
        fetched.append(request)
        return httpx.Response(200,json={'body':[{'from':0,'to':1,'content':'已核对'}]})
    with httpx.Client(transport=httpx.MockTransport(api)) as api_http, httpx.Client(transport=httpx.MockTransport(public)) as public_http:
        client=BilibiliClient(http=api_http,public_http=public_http,pace=lambda:None)
        client.key='fixture'
        with pytest.raises(PlatformError):client.transcript('BVfixture',2,1)
        assert not fetched


def test_explicit_url_mismatch_or_untrusted_host_stays_rejected():
    for url in ('https://aisubtitle.hdslb.com/hash?cid=99','https://evil.test/hash'):
        with pytest.raises(PlatformError):subtitle_url(url,1,2)


def test_matching_player_ids_do_not_allow_subtitle_beyond_video_duration():
    def api(request):
        return httpx.Response(200,json={'code':0,'data':{'bvid':'BVfixture','aid':1,'cid':2,
            'subtitle':{'subtitles':[{'lan':'zh','subtitle_url':'https://aisubtitle.hdslb.com/a?aid=1&cid=2'}]}}})
    def public(request):
        return httpx.Response(200,json={'body':[{'from':0,'to':1346,'content':'另一条影片'}]})
    with httpx.Client(transport=httpx.MockTransport(api)) as a, httpx.Client(transport=httpx.MockTransport(public)) as p:
        c=BilibiliClient(http=a,public_http=p,pace=lambda:None)
        with pytest.raises(PlatformError,match='超过'):
            c.transcript('BVfixture',2,1,duration=810)


def test_create_is_private_and_uses_csrf_without_secret_output():
    from urllib.parse import parse_qs

    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"id": 5}})

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    c.csrf = "fixture"
    assert c.create_folder("鸡汤")["id"] == 5
    form = parse_qs(calls[0].content.decode())
    assert form["privacy"] == ["1"] and form["csrf"] == ["fixture"]


@pytest.mark.parametrize("payload", [[], None, "bad"])
def test_invalid_write_response_is_uncertain_and_never_retried(payload):
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(200, text=__import__("json").dumps(payload))

    c = BilibiliClient(http=httpx.Client(transport=httpx.MockTransport(api)), pace=lambda: None)
    c.csrf = "fixture"
    with pytest.raises(PlatformError) as caught:
        c.create_folder("鸡汤")
    assert caught.value.kind == "write_uncertain" and len(calls) == 1
