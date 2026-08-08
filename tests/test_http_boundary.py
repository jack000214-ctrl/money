"""출처가 정상이 아닌 응답을 줬을 때 무엇을 하는가.

네트워크 자체는 테스트하지 않는다 — 출처가 내일 응답 모양을 바꾸는 것은 어차피
당하기 전엔 모른다. 여기서 고정하는 것은 **이미 당해 본 것들**이다. 이 저장소의
가드는 전부 사고 뒤에 생겼고(Yahoo 월봉 강등, KRX 스키마 변경, 다른 종목 혼입),
그 가드들이 리팩터로 조용히 죽으면 같은 사고가 그대로 반복된다.

세 가지가 특히 중요하다.

  1. **오류를 HTTP 200 안에 담아 보내는 출처가 있다.** data.go.kr 과 KRX 가
     그렇다. 상태코드만 보면 성공으로 읽힌다.
  2. **인증 실패의 원인이 둘이다.** 키가 틀린 것과 그 서비스에 활용신청이 안 된
     것은 고치는 방법이 다르므로 메시지가 갈라져야 한다.
  3. **인증키가 URL 에 실린다.** 오류 메시지에 URL 을 그대로 찍으면 키가 로그와
     화면에 남는다.
"""

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import dart, kr_lending, krx


class Response(io.BytesIO):
    """`urlopen` 의 컨텍스트 매니저 흉내."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def responds(payload, *, status=None):
    """정상 응답. `status` 를 주면 HTTPError 본문으로 돌려준다."""
    body = payload if isinstance(payload, bytes) else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    if status is None:
        return lambda *a, **k: Response(body)

    def raise_http(*a, **k):
        raise urllib.error.HTTPError(
            "https://example", status, "err", {}, Response(body))
    return raise_http


class Boundary(unittest.TestCase):
    def stderr_of(self, call, module):
        """`fail` 은 stderr 에 쓰고 종료한다. 나간 문구를 붙잡는다."""
        captured = io.StringIO()
        with patch("sys.stderr", captured), self.assertRaises(SystemExit):
            call()
        return captured.getvalue()


class TestDartResponses(Boundary):
    KEY = "SECRET-DART-KEY-0123456789"

    def request(self, payload, *, status=None, **kwargs):
        with patch("urllib.request.urlopen", responds(payload, status=status)):
            return dart.request("x.json", {}, self.KEY, **kwargs)

    def failure(self, payload, *, status=None, **kwargs):
        return self.stderr_of(
            lambda: self.request(payload, status=status, **kwargs), dart)

    def test_a_normal_payload_comes_through(self):
        data = self.request({"status": "000", "list": [{"a": 1}]})
        self.assertEqual(data["list"], [{"a": 1}])

    def test_an_http_error_never_prints_the_key(self):
        """키는 URL 쿼리에 실린다 — 메시지에 새면 로그에 남는다."""
        message = self.failure({}, status=500)
        self.assertNotIn(self.KEY, message)
        self.assertIn("HTTP 500", message)

    def test_a_connection_failure_is_reported(self):
        def boom(*a, **k):
            raise urllib.error.URLError("이름을 찾을 수 없다")
        with patch("urllib.request.urlopen", boom):
            message = self.stderr_of(
                lambda: dart.request("x.json", {}, self.KEY), dart)
        self.assertIn("연결 실패", message)

    def test_an_error_status_inside_a_200_is_caught(self):
        """DART 는 오류를 본문 status 로 준다. 상태코드는 200 이다."""
        self.assertIn("DART 응답", self.failure({"status": "020"}))

    def test_a_known_status_gets_its_explanation(self):
        """코드만 찍으면 무엇을 고쳐야 하는지 알 수 없다."""
        for code, expected in dart.STATUS_MESSAGES.items():
            if code in ("000", "013"):   # 성공, 그리고 결측을 뜻하는 코드
                continue
            with self.subTest(code):
                self.assertIn(expected, self.failure({"status": code}))

    def test_no_data_is_absence_not_failure_when_allowed(self):
        """분기 재무제표가 없는 것은 정상이다 — 없다는 사실을 돌려준다."""
        self.assertIsNone(
            self.request({"status": "013"}, allow_empty=True))

    def test_no_data_is_still_a_failure_when_not_allowed(self):
        self.assertIn("DART 응답 013", self.failure({"status": "013"}))

    def test_binary_payloads_skip_json_parsing(self):
        """XBRL 원본은 zip 이다. JSON 으로 읽으면 터진다."""
        blob = self.request(b"PK\x03\x04zip", binary=True)
        self.assertEqual(blob[:2], b"PK")

    def test_redaction_covers_any_message(self):
        self.assertNotIn(self.KEY, dart.redact(f"url?key={self.KEY}", self.KEY))

    def test_redaction_without_a_key_is_a_noop(self):
        self.assertEqual(dart.redact("본문", ""), "본문")


class TestKrxResponses(Boundary):
    def request(self, payload, *, status=None):
        with patch("urllib.request.urlopen", responds(payload, status=status)):
            return krx.request("svc", {}, "KEY")

    def failure(self, payload, *, status=None):
        return self.stderr_of(lambda: self.request(payload, status=status), krx)

    def test_a_normal_payload_comes_through(self):
        self.assertEqual(self.request({"respCode": "200", "OutBlock_1": []}),
                         {"respCode": "200", "OutBlock_1": []})

    def test_an_error_body_behind_an_http_error_is_still_read(self):
        """KRX 는 오류를 HTTP 4xx 본문에 JSON 으로 담는다."""
        self.assertIn("존재하지 않는 API 경로",
                      self.failure({"respCode": "404"}, status=404))

    def test_a_rejected_key_and_an_unsubscribed_service_differ(self):
        """둘 다 401 이지만 고치는 방법이 다르다."""
        rejected = self.failure({"respCode": "401", "respMsg": "Invalid Key"})
        unsubscribed = self.failure(
            {"respCode": "401", "respMsg": "not subscribed"})
        self.assertIn("인증키가 거부됐다", rejected)
        self.assertIn("활용신청", unsubscribed)
        self.assertIn(krx.SUBSCRIBE_URL, unsubscribed)

    def test_a_non_json_response_is_named_as_such(self):
        """장애 중에는 HTML 오류 페이지가 온다."""
        self.assertIn("JSON 이 아닌 응답",
                      self.failure(b"<html>maintenance</html>"))

    def test_any_other_error_code_is_surfaced(self):
        self.assertIn("KRX 응답 500",
                      self.failure({"respCode": "500", "respMsg": "내부 오류"}))

    def test_a_connection_failure_is_reported(self):
        def boom(*a, **k):
            raise urllib.error.URLError("timed out")
        with patch("urllib.request.urlopen", boom):
            self.assertIn("연결 실패", self.stderr_of(
                lambda: krx.request("svc", {}, "KEY"), krx))


class TestPublicDataResponses(Boundary):
    """data.go.kr 은 인증·한도 오류를 별도 봉투로 보낸다."""

    def failure(self, payload):
        def call():
            with patch("urllib.request.urlopen", responds(payload)):
                kr_lending.request({}, "KEY")
        return self.stderr_of(call, kr_lending)

    def envelope(self, err, reason="사유"):
        return {"OpenAPI_ServiceResponse": {
            "cmmMsgHeader": {"errMsg": err, "returnAuthMsg": reason}}}

    def test_an_unregistered_key_points_at_the_signup_page(self):
        message = self.failure(self.envelope("SERVICE_KEY_IS_NOT_REGISTERED"))
        self.assertIn("인증키가 등록되지 않았다", message)
        self.assertIn(kr_lending.SIGNUP_URL, message)

    def test_a_daily_quota_is_not_a_key_problem(self):
        """한도 초과를 키 문제로 읽으면 멀쩡한 키를 다시 발급받게 된다."""
        message = self.failure(
            self.envelope("LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS"))
        self.assertIn("일일 호출 한도", message)
        self.assertNotIn("인증키가 등록되지", message)

    def test_a_retired_service_path_is_named(self):
        self.assertIn("서비스 경로가 폐기됐다",
                      self.failure(self.envelope("NO_OPENAPI_SERVICE_ERROR")))

    def test_an_unknown_envelope_error_still_stops(self):
        self.assertIn("data.go.kr 오류",
                      self.failure(self.envelope("처음 보는 오류")))

    def test_a_non_json_response_shows_its_opening(self):
        """무엇이 왔는지 보여주지 않으면 원인을 못 찾는다."""
        message = self.failure(b"<html>\xec\x98\xa4\xeb\xa5\x98</html>")
        self.assertIn("JSON 이 아닌 응답", message)
        self.assertIn("<html>", message)


if __name__ == "__main__":
    unittest.main()
