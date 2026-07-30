"""모델이 보존하는 웹 원문의 회귀 테스트.

가장 위험한 실패는 수집이 실패하는 것이 아니라, 프레임 껍데기를 받아놓고
"원문을 보존했다"는 기록만 남기는 것이다. DART 뷰어 주소가 정확히 그렇다.
"""

import unittest

from collectors.web import _title, shell_page_warning

DART_VIEWER = """<html><head><title>삼성전자/연결재무제표기준영업(잠정)실적(공정공시)/2026.07.30</title>
<script>var currentDocValues = {rcpNo: '20260730800077'};</script></head>
<body><div id="north"></div><iframe id="ifrm" src="/report/viewer.do"></iframe></body></html>"""

ARTICLE = (
    "<html><head><title>기사 제목</title></head><body><article>"
    + "메모리 고정거래가격이 3분기에 정점을 찍었다는 관측이 나온다. " * 40
    + "</article><iframe src='//ads.example.invalid/banner'></iframe></body></html>"
)


class TestShellPageWarning(unittest.TestCase):
    def test_frame_shell_is_refused_as_evidence(self):
        warning = shell_page_warning(
            {"body": DART_VIEWER, "final_url": "https://dart.fss.or.kr/dsaf001/main.do"}
        )
        self.assertIsNotNone(warning)
        self.assertIn("인용하면 안 된다", warning)

    def test_dart_shell_points_at_the_real_path(self):
        warning = shell_page_warning(
            {"body": DART_VIEWER, "final_url": "https://dart.fss.or.kr/dsaf001/main.do"}
        )
        self.assertIn("collectors.dart document", warning)

    def test_article_with_an_ad_iframe_is_still_real_content(self):
        # 광고 iframe 하나 때문에 본문이 있는 기사를 껍데기로 판정하면
        # 모델이 멀쩡한 근거를 버린다.
        self.assertIsNone(
            shell_page_warning(
                {"body": ARTICLE, "final_url": "https://news.example.invalid/1"}
            )
        )

    def test_page_without_frames_is_never_a_shell(self):
        self.assertIsNone(
            shell_page_warning(
                {"body": "<html><body>짧다</body></html>", "final_url": "https://a.b"}
            )
        )


class TestTitle(unittest.TestCase):
    def test_reads_title(self):
        self.assertEqual(_title(ARTICLE), "기사 제목")

    def test_collapses_whitespace(self):
        self.assertEqual(_title("<title>\n  가  나\t</title>"), "가 나")

    def test_missing_title_is_none(self):
        self.assertIsNone(_title("<html><body>없다</body></html>"))


if __name__ == "__main__":
    unittest.main()
