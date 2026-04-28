from __future__ import annotations

from eimemory.intake.fulltext import extract_fulltext, parse_fulltext_document


def test_extract_fulltext_from_article_html_removes_chrome_and_metadata() -> None:
    html = """
    <!doctype html>
    <html>
      <head>
        <title>Browser title should lose suffix - Example</title>
        <meta name="author" content="Ada Lovelace">
        <meta property="article:published_time" content="2026-04-20T10:00:00Z">
        <meta property="og:title" content="Readable Systems Are Reliable Systems">
        <meta property="og:url" content="https://example.test/articles/readable-systems">
        <meta property="og:image" content="/images/hero.png">
        <link rel="canonical" href="https://example.test/articles/readable-systems">
      </head>
      <body>
        <nav>Home Pricing Login</nav>
        <article>
          <h1>Readable Systems Are Reliable Systems</h1>
          <p>Reliable systems begin with boring, explicit interfaces.</p>
          <p>They keep operators calm because the important state is visible.</p>
          <script>window.location = "https://tracker.example";</script>
          <style>.ad { display: block; }</style>
        </article>
        <footer>Copyright and unrelated links</footer>
      </body>
    </html>
    """

    result = extract_fulltext(
        "https://example.test/articles/readable-systems?utm_source=feed",
        html,
        source_kind="web",
    )

    assert result.ok is True
    assert result.title == "Readable Systems Are Reliable Systems"
    assert "Reliable systems begin" in result.text
    assert "important state is visible" in result.text
    assert "Home Pricing Login" not in result.text
    assert "window.location" not in result.text
    assert result.byline == "Ada Lovelace"
    assert result.date == "2026-04-20T10:00:00Z"
    assert result.canonical_url == "https://example.test/articles/readable-systems"
    assert result.images == ["https://example.test/images/hero.png"]
    assert result.quality_score >= 0.6
    assert result.error == ""


def test_parse_wechat_article_html_uses_js_content_container_without_bypass() -> None:
    html = """
    <html>
      <head>
        <meta property="og:title" content="微信里的长期主义">
        <meta name="author" content="宝玉">
        <meta property="og:image" content="https://mmbiz.qpic.cn/mmbiz_jpg/cover/0">
      </head>
      <body>
        <div id="js_article">
          <h1 id="activity-name">微信里的长期主义</h1>
          <span id="js_name">宝玉</span>
          <em id="publish_time">2026-04-21</em>
          <div id="js_content">
            <p>第一段说明文章的核心观点，强调慢变量的重要性。</p>
            <p>第二段继续展开，解释为什么稳定的信息结构更值得信任。</p>
            <img data-src="https://mmbiz.qpic.cn/mmbiz_png/body/0">
          </div>
        </div>
        <script nonce="abc">alert("do not run");</script>
      </body>
    </html>
    """

    result = parse_fulltext_document(
        html,
        url="https://mp.weixin.qq.com/s/example",
        source_kind="wechat",
    )

    assert result.ok is True
    assert result.title == "微信里的长期主义"
    assert "慢变量的重要性" in result.text
    assert "稳定的信息结构" in result.text
    assert "alert" not in result.text
    assert result.byline == "宝玉"
    assert result.date == "2026-04-21"
    assert result.images == [
        "https://mmbiz.qpic.cn/mmbiz_jpg/cover/0",
        "https://mmbiz.qpic.cn/mmbiz_png/body/0",
    ]
    assert result.meta["source_kind"] == "wechat"


def test_noise_page_is_low_quality_and_keeps_best_available_title() -> None:
    html = """
    <html>
      <head><title>Subscribe Now</title></head>
      <body>
        <header>Brand</header>
        <nav>Home About Pricing</nav>
        <main>
          <div class="signup">Subscribe now!</div>
          <p>Login to continue.</p>
        </main>
        <footer>Footer links</footer>
      </body>
    </html>
    """

    result = extract_fulltext("https://example.test/noise", html)

    assert result.ok is False
    assert result.title == "Subscribe Now"
    assert result.quality_score < 0.4
    assert result.error == "low quality content"


def test_empty_payload_returns_safe_error_shape() -> None:
    result = extract_fulltext("https://example.test/empty", "")

    assert result.ok is False
    assert result.title == ""
    assert result.text == ""
    assert result.byline == ""
    assert result.date == ""
    assert result.canonical_url == "https://example.test/empty"
    assert result.images == []
    assert result.meta["source_url"] == "https://example.test/empty"
    assert result.quality_score == 0.0
    assert result.error == "empty payload"
