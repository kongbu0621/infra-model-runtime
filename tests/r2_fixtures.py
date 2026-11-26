"""Synthetic endpoint and immutable R2 test inputs; no account credentials."""
from infra_model_runtime._boundary import encoded

def configuration(url='http://127.0.0.1:1/v1'):
    profiles = {}
    for name, adapter in (('openai', 'openai_responses'), ('local', 'openai_chat')):
        profiles[name] = dict(adapter=adapter, base_url=url, model='fixture-model', credential_ref=None,
            capability_profile='fixture-v1', max_input_bytes=65536, max_output_tokens=1024,
            timeout_seconds=3, adapter_options={})
    return dict(schema='model-runtime-routing-v1', enabled=True, active_profile='openai', profiles=profiles,
        policy=dict(budget_pool='default-budget', resource_group='default-resource', max_calls=100,
        max_input_bytes=1000000, max_output_tokens=100000, max_requests=64, max_queued=32, max_payload_bytes=134217728))

CONTRACT = encoded(dict(chat_output_parameter='max_tokens', context_tokens=1000000, input_tokens_per_byte=2, evidence_ref='synthetic-fixture'))

def response(adapter='openai_responses',text='{"ok":true}',output=2,**changes):
    if adapter=='openai_responses':
        value=dict(id='resp-fixture',object='response',model='fixture-model',status='completed',error=None,
            output=[dict(type='message',role='assistant',status='completed',content=[dict(type='output_text',text=text)])],
            usage=dict(input_tokens=10,output_tokens=output,total_tokens=10+output,output_tokens_details=dict(reasoning_tokens=0)))
    else:
        value=dict(id='chat-fixture',object='chat.completion',model='fixture-model',
            choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content=text))],
            usage=dict(prompt_tokens=10,completion_tokens=output,total_tokens=10+output))
    value.update(changes)
    return encoded(value)
