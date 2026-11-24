"""Generation-bound projection/control of the existing blocking approval queue.

Ordinary approvals and clarify prompts share the existing native waiters.
Secret/sudo prompts remain native; no response is published or journaled here.
"""
from copy import deepcopy

from gateway.session_prompt_hooks import PromptTable
from persistence.core.errors import AuthorityRefusal
from tools.approval import list_gateway_approvals, resolve_gateway_approval


def _approval_response(prompt, response):
    """Validate one answer without giving command cards the folder/site denial-reason channel."""
    if (not isinstance(response, dict) or set(response) not in ({'choice'}, {'choice', 'reason'})
            or response.get('choice') not in prompt['choices']):
        raise AuthorityRefusal('invalid_params')
    if 'reason' not in response:
        return response['choice'], None
    reason = response['reason']
    if (prompt.get('approval_kind') not in {'access', 'network'} or response['choice'] != 'deny'
            or not isinstance(reason, str) or len(reason) > 16384):
        raise AuthorityRefusal('invalid_params')
    return response['choice'], reason.strip() or None


class PendingControls:
    def __init__(self, events):
        self.events = events
        self.pending = PromptTable()  # a prompt leaving it is settled for observers
        self.remote_responders = {}

    def register(self, session_id, route, generation, data, *, timeout_s=None):
        from gateway.run import _redact_approval_command
        with self.events.lock:
            prompt_id = data['request_id']
            choices = ['once', 'deny']
            if data.get('allow_session', True) and not data.get('smart_denied', False):
                choices.append('session')
            if data.get('allow_permanent', True) and not data.get('smart_denied', False):
                choices.append('always')
            prompt = {'kind': 'approval', 'prompt_id': prompt_id,
                      'execution_generation': generation,
                      'command': _redact_approval_command(data.get('command', '')),
                      'description': _redact_approval_command(data.get('description', '')),
                      'choices': choices}
            # ``kind`` already names the prompt envelope; keep the card's kind distinct.
            if data.get('kind') in {'command', 'access', 'network'}:
                prompt['approval_kind'] = data['kind']
            prompt.update({field: _redact_approval_command(data[field])
                           for field in ('path', 'access', 'host', 'label', 'risk') if field in data})
            if 'edit' in data:
                prompt['edit'] = deepcopy(data['edit'])
                prompt['choices'] = ['once', 'deny']
            if timeout_s is not None:
                prompt['timeout_s'] = timeout_s
            self.pending[prompt_id] = (route, prompt)
            self.events.publish(session_id, prompt, event_type='approval.request')

    def register_clarify(self, session_id, generation, entry, *, timeout_s=None):
        from gateway.run import _redact_approval_command
        with self.events.lock:
            prompt = {"kind": "clarify", "prompt_id": entry.clarify_id,
                      "execution_generation": generation,
                      "question": _redact_approval_command(entry.question),
                      "choices": [_redact_approval_command(c) for c in entry.choices or []],
                      "multi_select": entry.multi_select}
            if timeout_s is not None:
                prompt["timeout_s"] = timeout_s
            self.pending[entry.clarify_id] = (entry, prompt)
            self.events.publish(session_id, prompt, event_type="clarify.request")

    def waiting_kinds(self):
        """Kinds of the prompts still parked on the user, without pruning or publishing (the
        teammate-wait strip polls this from another bot's chat; ``snapshot`` stays the viewer's)."""
        with self.events.lock:
            return sorted({prompt['kind'] for prompt_id, (route, prompt) in self.pending.items()
                           if prompt_id in self.remote_responders or (
                               not route.event.is_set() if prompt['kind'] == 'clarify' else
                               any(p['request_id'] == prompt_id for p in list_gateway_approvals(route)))})

    def snapshot(self, session_id, generation):
        with self.events.lock:
            for prompt_id, (route, prompt) in list(self.pending.items()):
                active = (prompt_id in self.remote_responders or
                          (not route.event.is_set() if prompt['kind'] == 'clarify' else
                           any(p['request_id'] == prompt_id for p in list_gateway_approvals(route))))
                if prompt['execution_generation'] != generation or not active:
                    del self.pending[prompt_id]
                    self.remote_responders.pop(prompt_id, None)
                    self.events.publish(session_id, {'prompt_id': prompt_id,
                        'execution_generation': prompt['execution_generation']}, event_type=prompt['kind'] + '.settled')
            return tuple(deepcopy(prompt) for _, prompt in self.pending.values())

    def respond(self, session_id, generation, prompt_id, response, *, kind="approval"):
        with self.events.lock:
            self.snapshot(session_id, generation)
            entry = self.pending.get(prompt_id)
            if entry is None:
                return {'status': 'already_resolved', 'prompt_id': prompt_id}
            route, prompt = entry
            if prompt['kind'] != kind:
                raise AuthorityRefusal('invalid_params')
            if prompt_id in self.remote_responders:
                if kind == 'approval':
                    choice, reason = _approval_response(prompt, response)
                    if reason is None:
                        self.remote_responders[prompt_id](kind, prompt_id, choice)
                    else:
                        self.remote_responders[prompt_id](kind, prompt_id, choice, reason)
                else:
                    if (not isinstance(response, dict) or set(response) != {'answer'}
                            or not isinstance(response['answer'], str) or len(response['answer']) > 16384):
                        raise AuthorityRefusal('invalid_params')
                    self.remote_responders[prompt_id](kind, prompt_id, response['answer'])
                del self.remote_responders[prompt_id]
                del self.pending[prompt_id]
                self.events.publish(session_id, {'prompt_id': prompt_id, 'execution_generation': generation},
                                    event_type=kind + '.settled')
                return {'status': 'resolved', 'prompt_id': prompt_id}
            if kind == 'clarify':
                from tools.clarify_gateway import SKIPPED, resolve_gateway_clarify
                if (not isinstance(response, dict) or set(response) != {'answer'}
                        or not isinstance(response['answer'], str)):
                    raise AuthorityRefusal('invalid_params')
                # An empty answer is the documented skip; the waiter reads a bare "" as a timeout.
                resolved = resolve_gateway_clarify(prompt_id, response['answer'] if response['answer'].strip() else SKIPPED)
                self.snapshot(session_id, generation)
                return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}
            choice, reason = _approval_response(prompt, response)
            # The tools lock arbitrates native taps versus attached responders;
            # an exact request ID can never consume the next FIFO approval.
            resolved = resolve_gateway_approval(route, choice, request_id=prompt_id,
                                                **({'reason': reason} if reason is not None else {}))
            self.snapshot(session_id, generation)
            return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}
