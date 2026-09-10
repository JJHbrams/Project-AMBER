"""No real provider launch: diagnostic privacy and exit-code regression."""
import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'real_provider_probe', Path(__file__).parents[1] / 'scripts/dev/real_provider_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_final_phase_failure_is_not_process_success():
    assert not probe.provider_execution_ok({'turns': [{'exit_code': 0}, {'exit_code': 1}]}, 2)
    assert not probe.provider_execution_ok({'turns': [{'exit_code': 1}]}, 1)
    assert not probe.provider_execution_ok({'turns': [{'exit_code': 0}]}, 2)
    assert probe.provider_execution_ok({'turns': [{'exit_code': 0}]}, 1)


def test_title_delivery_strips_unrelated_content():
    result = probe.title_delivery({'structuredContent': {
        'accepted': True, 'reason': 'delivered', 'private': 'PRIVATE_CANARY'}})
    assert result == {'accepted': True, 'reason': 'delivered'}
    assert 'PRIVATE_CANARY' not in json.dumps(result)
    assert probe.title_delivery({'accepted': True, 'reason': 'PRIVATE_CANARY'}) is None


def test_event_never_returns_message_body():
    event = probe.extract_event({'type': 'assistant', 'message': {
        'content': [{'type': 'text', 'text': 'PRIVATE_CANARY'}]}})
    assert 'PRIVATE_CANARY' not in json.dumps(event)


def test_generated_title_is_correlated_by_hash_only():
    event = probe.extract_event({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'name': 'mcp__engram__engram_report_session_title',
         'input': {'title': 'PRIVATE_CANARY'}}]}})
    assert len(event['title_hashes']) == 1
    assert len(event['title_hashes'][0]) == 64
    assert 'PRIVATE_CANARY' not in json.dumps(event)


def test_hook_diagnostics_do_not_return_arbitrary_outputs():
    event = probe.extract_event({'type': 'system', 'subtype': 'hook_response',
        'hook_event': 'UserPromptSubmit', 'stdout': 'PRIVATE_CANARY',
        'stderr': 'PRIVATE_CANARY not connected', 'exit_code': 1,
        'outcome': 'error'})
    assert 'PRIVATE_CANARY' not in json.dumps(event)
    assert event['hook_diagnostics'][0]['outcome'] == 'error'


def test_codex_title_arguments_are_hashed_not_retained():
    for arguments in ({'title': 'PRIVATE_CANARY'}, '{"title":"PRIVATE_CANARY"}'):
        event = probe.extract_event({'type': 'item.completed', 'item': {
            'type': 'mcp_tool_call', 'tool': 'engram_report_session_title',
            'arguments': arguments, 'result': {'structuredContent': {
                'accepted': True, 'reason': 'delivered'}}}})
        assert len(event['title_hashes']) == 1
        assert event['title_delivery'] == [{'accepted': True, 'reason': 'delivered'}]
        assert 'PRIVATE_CANARY' not in json.dumps(event)


def test_hook_trust_warning_is_sanitized():
    assert 'hook_trust' in probe.error_categories('Some hooks need review. Open /hooks. PRIVATE_CANARY')


def test_selected_event_observer_retains_only_finite_metadata():
    observer_spec = importlib.util.spec_from_file_location('selected_event_observer',
        Path(__file__).parents[1] / 'scripts/dev/selected_event_observer.py')
    module = importlib.util.module_from_spec(observer_spec)
    observer_spec.loader.exec_module(module)
    observer = object.__new__(module.SelectedEventObserver)
    observer.selected, observer.events = None, []
    observer.accept({'type': 'session.state_changed', 'payload': {'session': {
        'provider': 'mcp', 'session_id': 'a' * 32, 'label': 'PRIVATE_CANARY'}}}, 0)
    observer.accept({'type': 'tool.started', 'display_hint': 'search', 'payload': {
        'category': 'search', 'input': 'PRIVATE_CANARY'}}, 1)
    observer.accept({'type': 'tool.started', 'display_hint': 'PRIVATE_CANARY',
                     'payload': {}}, 2)
    assert len(observer.events) == 1
    assert observer.events[0]['hint'] == 'search'
    assert 'PRIVATE_CANARY' not in json.dumps(vars(observer))
