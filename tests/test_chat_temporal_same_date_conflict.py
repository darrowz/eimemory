"""Explicit same-effective-date conflict policy; no capture/model winner."""
from types import SimpleNamespace
import pytest
from test_ordinary_chat_temporal_semantics import chat,SourceEchoModel


def omit_matching(monkeypatch, marker):
    original=SourceEchoModel.complete
    def complete(self,**kwargs):
        prompt=kwargs['user_prompt']
        if not prompt.startswith('{'):
            user=prompt.split('\nUSER:\n',1)[1].split('\n\nASSISTANT:\n',1)[0]
            if marker in user:return SimpleNamespace(text='[]')
        return original(self,**kwargs)
    monkeypatch.setattr(SourceEchoModel,'complete',complete)


@pytest.mark.parametrize('omitted_first',[False,True])
def test_equal_date_contradiction_has_no_model_or_capture_winner(chat,monkeypatch,omitted_first):
    omit_matching(monkeypatch,'乙员')
    known='自2020-01-01起，同日项目负责人是甲员。'
    omitted='自2020-01-01起，同日项目负责人是乙员。'
    for text in ([omitted,known] if omitted_first else [known,omitted]):chat.say(text)
    assert not chat.recall('同日项目现在的负责人是谁？')
    assert not chat.recall('在2020-01-01，同日项目负责人是谁？')
    chat.close();chat.open()
    assert not chat.recall('同日项目现在的负责人是谁？')


def test_equal_date_same_value_corroboration_is_not_a_conflict(chat,monkeypatch):
    chat.say('自2020-01-01起，同日项目负责人是甲员。')
    omit_matching(monkeypatch,'甲员')
    chat.say('自2020-01-01起，同日项目负责人是甲员。')
    assert any('甲员' in x.get('summary','') for x in chat.recall('同日项目现在的负责人是谁？'))


def test_older_effective_omission_cannot_override_later_known_state(chat,monkeypatch):
    chat.say('自2020-01-01起，同日项目负责人是甲员。')
    omit_matching(monkeypatch,'乙员')
    chat.say('自2019-01-01起，同日项目负责人是乙员。')
    assert any('甲员' in x.get('summary','') for x in chat.recall('同日项目现在的负责人是谁？'))


def test_other_attribute_equal_date_omission_does_not_suppress_owner(chat,monkeypatch):
    chat.say('自2020-01-01起，同日项目负责人是甲员。')
    omit_matching(monkeypatch,'乙渠道')
    chat.say('自2020-01-01起，同日项目通知渠道是乙渠道。')
    assert any('甲员' in x.get('summary','') for x in chat.recall('同日项目现在的负责人是谁？'))


def test_equal_start_nonoverlapping_pending_interval_leaves_current_state(chat,monkeypatch):
    chat.say('自2020-01-01起，同日项目负责人是甲员。')
    omit_matching(monkeypatch,'乙员')
    chat.say('从2020-01-01到2021-01-01，同日项目负责人是乙员。')
    assert not chat.recall('在2020-06-01，同日项目负责人是谁？')
    assert any('甲员' in x.get('summary','') for x in chat.recall('同日项目现在的负责人是谁？'))
