import pytest
from services.incident_replay import prepare, validate_report, investigate, _parse_json


def test_missing_and_oversized():
    with pytest.raises(ValueError):
        prepare('alert', '')
    with pytest.raises(ValueError):
        prepare('alert', 'x' * 12001)


def test_redacts_and_numbers():
    result = prepare('alert', 'password=secret123\n\nERROR connection refused')
    assert result['evidence'][0]['text'] == 'password=[REDACTED]'
    assert result['evidence'][1]['id'] == 2


def test_rejects_fake_citations():
    result = validate_report({
        'observations': [
            {'observation': 'failure', 'evidence_ids': [999]},
            {'observation': 'refused', 'evidence_ids': [1]},
        ]
    }, [{'id': 1, 'text': 'connection refused'}])
    assert len(result['observations']) == 1
    assert result['observations'][0]['evidence_ids'] == [1]


def test_uses_aurora_model_factory_without_tools():
    seen = {}

    class FakeModel:
        def invoke(self, messages, config=None):
            seen['messages'] = messages
            seen['config'] = config
            return type('Response', (), {'content': '{"summary":"Possible failure","observations":[],"hypotheses":[],"next_checks":[],"limitations":"Unverified"}'})()

    def fake_factory(**kwargs):
        seen.update(kwargs)
        return FakeModel()

    result = investigate('Alert', 'ERROR 503', model_factory=fake_factory, model_name='google/gemini-3.8-flash')
    assert seen['model'] == 'google/gemini-3.8-flash'
    assert seen['streaming'] is False
    assert seen['messages'][0][0] == 'system'
    assert seen['messages'][1][0] == 'human'
    assert len(seen['messages']) == 2
    assert result['evidence_only'] is True


def test_json_fenced_model_response():
    assert _parse_json('```json\n{"summary":"ok"}\n```')['summary'] == 'ok'


def test_invalid_model_response_fails_closed():
    with pytest.raises(ValueError):
        _parse_json('not json')


def test_gemini_thinking_and_text_blocks():
    content = [
        {"type": "thinking", "thinking": "Do not parse this private trace"},
        {"type": "text", "text": '{"summary":"Database connection failure"}', "extras": {"signature": "test"}},
    ]
    assert _parse_json(content)["summary"] == "Database connection failure"


def test_gemini_blocks_work_through_investigate():
    class FakeModel:
        def invoke(self, messages, config=None):
            content = [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": '{"summary":"Failure", "observations":[{"observation":"503", "evidence_ids":[1]}], "hypotheses":[], "next_checks":[], "limitations":"Unverified"}'},
            ]
            return type("Response", (), {"content": content})()

    result = investigate("Alert", "ERROR 503", model_factory=lambda **kwargs: FakeModel(), model_name="google/gemini-3.8-flash")
    assert result["summary"] == "Failure"
    assert result["observations"][0]["evidence_ids"] == [1]
