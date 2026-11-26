"""Synthetic credential echoes must be rejected even in malformed captures."""
import json
import unittest
from unittest.mock import Mock, patch
from infra_model_runtime import transport


def unicode_escape(value):
    return ''.join('\\u%04x' % ord(char) for char in value).encode('ascii')


def damaged_captures(secret):
    echo=b'"echo":"'+unicode_escape(secret)+b'"'
    return {
        'truncated': b'{'+echo,
        'duplicate': b'{"x":0,"x":1,'+echo+b'}',
        'invalid-utf8': b'{"bad":"\xff",'+echo+b'}',
        'trailing-data': b'{'+echo+b'} extra',
        'deep': b'['*18+b'{'+echo+b'}'+b']'*18,
        'many-nodes': b'{"items":['+b'0,'*16400+b'0],'+echo+b'}',
    }


class SecretCaptureTests(unittest.TestCase):
    secret='synthetic-review-token'

    def invoke(self,raw):
        child=Mock(returncode=0)
        child.communicate.return_value=(b'{"status":200,"complete":true,"sent":"response_received"}\n'+raw,b'')
        with patch.object(transport.subprocess,'Popen',return_value=child) as spawn:
            meta,found=transport.invoke(dict(base_url='http://127.0.0.1:1/v1',timeout_seconds=1),'/responses',b'{}',self.secret)
        self.assertEqual(spawn.call_count,1)
        self.assertEqual(child.communicate.call_count,1)
        return meta,found

    def rejected(self,raw):
        meta,found=self.invoke(raw)
        self.assertEqual(found,b'','encoded credential escaped the capture boundary')
        self.assertEqual(meta,dict(status=200,complete=False,sent='response_received'))

    def test_invalid_json_cannot_hide_escaped_credential(self):
        for name,raw in damaged_captures(self.secret).items():
            with self.subTest(case=name):self.rejected(raw)

    def test_embedded_or_diagnostic_text_cannot_hide_escaped_credential(self):
        escaped=unicode_escape(self.secret).decode('ascii')
        for text in ('{"echo":"'+escaped+'"',
                     '{"x":0,"x":1,"echo":"'+escaped+'"}',
                     'peer diagnostic: '+escaped,
                     '["'+escaped+'"] trailing'):
            with self.subTest(text_kind=text[:15]):
                self.rejected(json.dumps({'output_text':text}).encode())

    def test_supported_escape_spellings_are_rejected(self):
        self.secret=''.join(chr(c) for c in range(33,127))
        escaped=unicode_escape(self.secret)
        mixed=b''.join(unicode_escape(c) if i%2 else json.dumps(c)[1:-1].encode() for i,c in enumerate(self.secret))
        bodies=(json.dumps({'echo':self.secret}).encode(),
                b'{"echo":"'+escaped+b'"}',
                b'{"echo":"'+mixed+b'"}',
                b'{"echo":"'+b''.join(('\\u%04X'%ord(c)).encode() for c in self.secret)+b'"}')
        for index,raw in enumerate(bodies):
            for layer in (1,2):
                with self.subTest(spelling=index,layer=layer):
                    self.rejected(raw if layer==1 else json.dumps({'output_text':raw.decode()}).encode())

    def test_real_http_capture_checks_encoded_echo_for_both_protocols(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        secret=self.secret
        for suffix,short in (('/responses',False),('/chat/completions',True)):
            with self.subTest(suffix=suffix):
                calls=[];raw=b'{"echo":"'+unicode_escape(secret)+b'"} trailing'
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self,*args):pass
                    def do_POST(self):
                        calls.append((self.path,self.rfile.read(int(self.headers['Content-Length'])),self.headers.get('Authorization')))
                        self.send_response(200)
                        self.send_header('Content-Length',str(len(raw)+int(short)))
                        self.end_headers();self.wfile.write(raw);self.close_connection=True
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
                try:
                    meta,found=transport.invoke(dict(base_url=f'http://127.0.0.1:{server.server_port}/v1',timeout_seconds=2),suffix,b'{}',secret)
                    self.assertEqual(found,b'')
                    self.assertEqual(meta,dict(status=200,complete=False,sent='response_received'))
                    self.assertEqual(calls,[('/v1'+suffix,b'{}','Bearer '+secret)])
                finally:
                    server.shutdown();server.server_close();worker.join(3)

    def test_nonsecret_responses_remain_byte_exact(self):
        for raw in (b'{"ok":true}',b'{"echo":"different-token"}',b'{"unfinished":',
                    b'{"x":0,"x":1}',b'\xff',b'x'*262144,
                    b'{"text":"\\u4e2d\\ud83d\\ude00"}'):
            with self.subTest(size=len(raw),prefix=raw[:20]):
                meta,found=self.invoke(raw)
                self.assertEqual(found,raw)
                self.assertEqual(meta,dict(status=200,complete=True,sent='response_received'))


class DurableSecretCaptureTests(unittest.TestCase):
    def test_redacted_capture_stays_unknown_and_never_resends(self):
        from pathlib import Path
        import os,tempfile,time
        from infra_model_runtime import Generation,ModelRuntime
        from infra_model_runtime._boundary import encoded
        from r2_fixtures import configuration,CONTRACT,response
        secret='synthetic-durable-token'
        captures=damaged_captures(secret)
        for name in ('openai','local'):
            for case in ('truncated','duplicate','embedded'):
                with self.subTest(profile=name,case=case),tempfile.TemporaryDirectory() as directory:
                    path=Path(directory)/'runtime.sqlite';cfg=configuration();cfg['active_profile']=name
                    cfg['profiles'][name]['credential_ref']='env:R2_SYNTHETIC_CAPTURE_SECRET'
                    raw=captures.get(case)
                    if case=='embedded':
                        raw=response(cfg['profiles'][name]['adapter'],text='diagnostic '+unicode_escape(secret).decode())
                    child=Mock(returncode=0)
                    child.communicate.return_value=(b'{"status":200,"complete":true,"sent":"response_received"}\n'+raw,b'')
                    with patch.dict(os.environ,{'R2_SYNTHETIC_CAPTURE_SECRET':secret}),patch.object(transport.subprocess,'Popen',return_value=child) as spawn:
                        runtime=ModelRuntime.create(path,encoded(cfg))
                        runtime.approve('approve',name,account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
                        request=Generation('example','job','answer','Synthetic',(name,),32,time.time_ns()//1000000+60000)
                        receipt=runtime.submit(request);wire=runtime.read_payload('example','job','wire')
                        state=runtime.run('example','job')
                        self.assertEqual(runtime.read_payload('example','job','response'),b'')
                        self.assertNotIn(unicode_escape(secret),path.read_bytes())
                        self.assertNotIn(secret.encode(),path.read_bytes())
                        self.assertEqual((state.call.outcome,state.capture_complete,state.remote_state,state.code),('unknown',False,'unknown','OUTCOME_UNKNOWN'))
                        budget=runtime.budget()
                        self.assertEqual((budget.issued_calls,budget.input_bytes,budget.output_used,budget.output_reserved,budget.execution_occupied),(1,len(wire),0,32,1))
                        reopened=ModelRuntime(path)
                        self.assertEqual(reopened.submit(request),receipt)
                        self.assertEqual(reopened.run('example','job'),state)
                        self.assertEqual(reopened.recover('example','job'),state)
                        self.assertEqual(reopened.budget(),budget)
                        self.assertEqual(reopened.read_payload('example','job','response'),b'')
                        self.assertEqual(spawn.call_count,1)


if __name__=='__main__':unittest.main()
