"""Run only a synthetic loopback server; usable outside the source checkout.

python serial_consumer.py
No provider account, secret, private application, or existing state is required.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from infra_model_runtime import Generation, ModelRuntime


def main():
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            calls.append((self.path,self.rfile.read(int(self.headers['Content-Length']))))
            if self.path.endswith('/responses'):
                value=dict(id='synthetic-response',object='response',model='synthetic',status='completed',
                    output=[dict(type='message',role='assistant',status='completed',content=[dict(type='output_text',text='{"ok":true}')])],
                    usage=dict(input_tokens=10,output_tokens=2,total_tokens=12))
            else:
                value=dict(id='synthetic-chat',object='chat.completion',model='synthetic',
                    choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content='{"ok":true}'))],
                    usage=dict(prompt_tokens=10,completion_tokens=2,total_tokens=12))
            raw=json.dumps(value).encode()
            self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01),daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            endpoint='http://127.0.0.1:'+str(server.server_port)+'/v1'
            profiles={name:dict(adapter=adapter,base_url=endpoint,model='synthetic',credential_ref=None,
                capability_profile='synthetic-json-v1',max_input_bytes=65536,max_output_tokens=128,timeout_seconds=3,adapter_options={})
                for name,adapter in [('local','openai_chat'),('commercial','openai_responses')]}
            cfg=dict(schema='model-runtime-routing-v1',enabled=True,active_profile='local',profiles=profiles,
                policy=dict(budget_pool='example-budget',resource_group='example-resource',max_calls=2,
                max_input_bytes=65536,max_output_tokens=128,max_requests=8,max_queued=8,max_payload_bytes=4194304))
            encode=lambda value:json.dumps(value).encode()
            path=Path(directory)/'runtime.sqlite'
            runtime=ModelRuntime.create(path,encode(cfg))
            contract=encode(dict(chat_output_parameter='max_tokens',context_tokens=262144,input_tokens_per_byte=2,evidence_ref='local-synthetic-fixture'))
            for name in profiles:
                runtime.approve('approve-'+name,name,account='synthetic-only',service_revision='fixture-v1',capability_contract=contract)
            results=[]
            for index,name in enumerate(profiles):
                if index:
                    cfg['active_profile']=name;runtime.configure('switch',1,encode(cfg))
                req=Generation('example',name,'answer','Return a synthetic JSON object.',tuple(profiles),32,time.time_ns()//1000000+10000)
                receipt=runtime.submit(req);runtime.run('example',name)
                runtime=ModelRuntime(path)
                if runtime.submit(req)!=receipt:raise RuntimeError('Receipt changed')
                runtime.run('example',name)
                results.append(json.loads(runtime.read_result('example',name)))
            budget=runtime.budget()
            if len(calls)!=2 or budget.issued_calls!=2 or budget.input_bytes!=sum(len(raw) for _,raw in calls):
                raise RuntimeError('Send count or wire accounting mismatch')
            print(json.dumps(dict(protocols=[path for path,_ in calls],sends=len(calls),results=results,
                output_used=budget.output_used,output_reserved=budget.output_reserved,execution_occupied=budget.execution_occupied),ensure_ascii=False))
    finally:
        server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':main()
