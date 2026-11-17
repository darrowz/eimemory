"""Source clauses preserve literal dot tokens without mixing release branches."""

import pytest

from eimemory.raw.applicability import assess_applicability


CASES = [
    pytest.param(
        "澄汐版本6.1导出的包统一使用扩展名.rpk",
        "澄汐版本6.2导出的包统一使用扩展名.qbz",
        "。",
        ("澄汐版本6.1导出的包使用什么扩展名？", "澄汐版本6.2导出的包使用什么扩展名？"),
        id="zh-extension",
    ),
    pytest.param(
        "Coppercrest v8.2 exports archives using extension .slx",
        "Coppercrest v8.3 exports archives using extension .tnq",
        ". ",
        ("Which archive extension does Coppercrest v8.2 use?",
         "Which archive extension does Coppercrest v8.3 use?"),
        id="en-extension",
    ),
    pytest.param(
        "澄汐版本6.1读取隐藏文件.envrc",
        "澄汐版本6.2读取隐藏文件.profile",
        "。",
        ("澄汐版本6.1读取哪个隐藏文件？", "澄汐版本6.2读取哪个隐藏文件？"),
        id="zh-hidden-file",
    ),
    pytest.param(
        "Coppercrest v8.2 reads the hidden file .envrc",
        "Coppercrest v8.3 reads the hidden file .profile",
        ". ",
        ("Which hidden file does Coppercrest v8.2 read?",
         "Which hidden file does Coppercrest v8.3 read?"),
        id="en-hidden-file",
    ),
    pytest.param(
        "澄汐版本6.1必须设置属性settings._cache为strict",
        "澄汐版本6.2必须设置属性settings._cache为relaxed",
        "。",
        ("澄汐版本6.1应怎样设置缓存属性？", "澄汐版本6.2应怎样设置缓存属性？"),
        id="zh-underscore-property",
    ),
    pytest.param(
        "Coppercrest v8.2 sets settings._cache to strict",
        "Coppercrest v8.3 sets settings._cache to relaxed",
        ". ",
        ("How does Coppercrest v8.2 configure its cache setting?",
         "How does Coppercrest v8.3 configure its cache setting?"),
        id="en-underscore-property",
    ),
    pytest.param(
        "澄汐版本6.1必须设置属性network.timeout为short",
        "澄汐版本6.2必须设置属性network.timeout为long",
        "。",
        ("澄汐版本6.1应怎样设置超时属性？", "澄汐版本6.2应怎样设置超时属性？"),
        id="zh-existing-dotted-property",
    ),
    pytest.param(
        "Coppercrest v8.2 stores cache.snapshot in the archive directory",
        "Coppercrest v8.3 stores cache.snapshot in the staging directory",
        ". ",
        ("Where does Coppercrest v8.2 store its snapshot?",
         "Where does Coppercrest v8.3 store its snapshot?"),
        id="en-existing-dotted-file",
    ),
]


def _check(text, query):
    return assess_applicability({"status": "active", "content": {"text": text}}, query=query)


@pytest.mark.parametrize("first,second,separator,queries", CASES)
@pytest.mark.parametrize("requested_branch", [0, 1], ids=["earlier-release", "later-release"])
def test_dot_token_is_complete_and_other_release_is_excluded(
    first, second, separator, queries, requested_branch,
):
    text = first + separator + second + separator.strip()
    decision = _check(text, queries[requested_branch])

    assert decision.status == "applicable"
    # The full assertion, not just the release prefix, is necessary answer data.
    # Exact original spans also establish that neither release branch leaks.
    assert [text[start:end] for start, end in decision.spans] == [
        (first, second)[requested_branch]
    ]


@pytest.mark.parametrize("text,query,expected", [
    pytest.param(
        "Coppercrest v8.2 requires stable mode. The file .service is only a sample. "
        "Coppercrest v8.3 requires debug mode.",
        "Which mode does Coppercrest v8.2 require?",
        "Coppercrest v8.2 requires stable mode",
        id="en-unbound-sentence",
    ),
    pytest.param(
        "澄汐版本6.1必须使用稳定模式。隐藏文件.envrc仅作通用示例。澄汐版本6.2必须使用调试模式。",
        "澄汐版本6.1必须使用什么模式？",
        "澄汐版本6.1必须使用稳定模式",
        id="zh-unbound-sentence",
    ),
])
def test_dot_token_does_not_inherit_version_across_real_sentence_boundary(text, query, expected):
    decision = _check(text, query)
    assert decision.status == "applicable"
    assert [text[start:end] for start, end in decision.spans] == [expected]
