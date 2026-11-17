"""Release identity versus request grammar; no frozen benchmark entities."""
import pytest
from eimemory.raw.applicability import assess_applicability


def check(text,query,status='active'):
    return assess_applicability({'status':status,'content':{'text':text}},query=query)


@pytest.mark.parametrize('owner,product',[('清河网关','Piper'),('竹溪节点','Sable')])
def test_running_relation_and_possessive_are_not_owner_identity(owner,product):
    text=f'{owner}的{product} 6.x使用P6。{product} 7.x使用P7。'
    result=check(text,f'{owner}仍运行{product} 6.4，应使用什么标记？')
    assert result.status=='applicable'
    assert [text[a:b] for a,b in result.spans]==[f'{owner}的{product} 6.x使用P6']
    assert check(text,f'另一网关仍运行{product} 6.4，应使用什么标记？').status!='applicable'
    assert check(text,f'{owner}仍运行Other 6.4，应使用什么标记？').status!='applicable'


@pytest.mark.parametrize('prefix',['After 4 consecutive checks','Before 3 failed attempts','Within 9 samples'])
def test_condition_counts_do_not_invent_a_product_version(prefix):
    assert check('Nacre changes routing when the threshold is reached.',prefix+', what should Nacre do?').status=='applicable'


def test_explicit_release_is_not_removed_by_count_grammar():
    assert check('Nacre v3 supports the feature.','After version 4, can Nacre use it?').status!='applicable'
    assert check('After 3 supports the feature.','After 4 supports which feature?').status!='applicable'


@pytest.mark.parametrize('question',['Is the integrity flag optional for Nacre version 8.0?',
                                    'Which output field is required by Nacre version 8.0?'])
def test_attributes_before_product_do_not_become_product_identity(question):
    assert check('Nacre version 8.0 requires the integrity flag.',question).status=='applicable'
    assert check('Nacre version 9.0 requires the integrity flag.',question).status=='not_applicable'
    assert check('Other version 8.0 requires the integrity flag.',question).status!='applicable'


def test_calendar_inventory_qualifier_does_not_replace_explicit_release():
    query='The April 9 inspection says our Nacre package reports version 2031.2. Which marker applies?'
    assert check('Nacre package version 2031.2 requires N-A.',query).status=='applicable'
    assert check('Nacre package version 2031.3 requires N-B.',query).status=='not_applicable'
    assert check('Nacre package version 2031.2 requires N-A.',query,'deprecated').status=='not_applicable'
    assert check('Status: withdrawn\nNacre package version 2031.2 requires N-A.',query).status=='not_applicable'


def test_month_named_product_without_calendar_qualifier_remains_a_release():
    assert check('April 8 requires an older field.','April 9 requires what field?').status=='not_applicable'
    assert check('April version 8 requires an older field.','The April version 9 requires what field?').status=='not_applicable'


@pytest.mark.parametrize('query',['Where are Orion SDK 4 manifests?', 'where are orion sdk 4 manifests?'])
def test_nearest_identity_retains_compound_project_name(query):
    assert check('Orion SDK 4 stores manifests in Git.',query).status=='applicable'
    assert check('Vega SDK 4 stores manifests elsewhere.',query).status!='applicable'
