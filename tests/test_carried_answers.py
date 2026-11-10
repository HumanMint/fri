from types import SimpleNamespace

from fri.fri import Fri


def test_request_boundary_accumulates_only_remaining_answers():
    cog = SimpleNamespace(capabilities={'saved-answer-ids'}, dependencies=lambda context: set())
    context = {'fence': {'turn': {}}, 'turns': [{'answer_id': '902', 'reply': 'answer'}]}
    Fri.depend(cog, context)
    # A later request has withheld B and only carries another answer.
    context['turns'] = [{'answer_id': '903', 'reply': 'next'}]
    Fri.depend(cog, context)
    assert Fri.drawn_on(cog, context)['carried_answers'] == ['902', '903']


def test_withheld_or_malformed_answer_not_reported():
    cog = SimpleNamespace(capabilities={'saved-answer-ids'}, dependencies=lambda context: set())
    context = {'fence': {'turn': {}}, 'turns': [{'input': 'B question'}, {'answer_id': {}, 'reply': 'x'}]}
    Fri.depend(cog, context)
    assert Fri.drawn_on(cog, context)['carried_answers'] == []
    assert Fri.drawn_on(cog, context, False)['carried_answers'] == []


def test_old_gene_gets_no_new_field():
    cog = SimpleNamespace(capabilities=set(), dependencies=lambda context: set())
    context = {'fence': {'turn': {}}, 'turns': [{'answer_id': '902', 'reply': 'answer'}]}
    Fri.depend(cog, context)
    assert Fri.drawn_on(cog, context) == {}
