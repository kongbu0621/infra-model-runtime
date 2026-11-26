"""Real loopback sockets and installed-package child processes; no real model."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from infra_model_runtime import ModelRuntime, Generation
from infra_model_runtime import transport, protocol
from infra_model_runtime._boundary import encoded
from r2_fixtures import response, configuration, CONTRACT


@contextmanager
def server(mode='ok', message=None):
    calls=[];stop=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            raw=self.rfile.read(int(self.headers['Content-Length']))
            calls.append((self.path,raw,dict(self.headers)))
            adapter='openai_responses' if self.path.endswith('/responses') else 'openai_chat'
            payload=response(adapter)
            try:
                if message is not None:self.connection.sendall(message)
                else:
                    if mode=='oversize':payload=b'x'*262145
                    if mode=='exact':payload=b'x'*262144
                    if mode=='echo':payload=response(adapter,text=json.dumps({'echo':self.headers['Authorization']}))
                    self.send_response(302 if mode=='redirect' else 200)
                    self.send_header('Content-Length',str(len(payload)+(10 if mode=='short' else 0)))
                    if mode=='redirect':self.send_header('Location','http://127.0.0.1:1/no-redirect')
                    self.end_headers()
                    if mode=='stall':stop.wait(5)
                    else:self.wfile.write(payload)
            except (BrokenPipeError,ConnectionResetError):pass
            self.close_connection=True
    http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=lambda:http.serve_forever(poll_interval=0.01),daemon=True);thread.start()
    try:yield 'http://127.0.0.1:'+str(http.server_port)+'/v1',calls
    finally:stop.set();http.shutdown();http.server_close();thread.join(3)


class HTTPTests(unittest.TestCase):
    def invoke(self,url,secret=None,timeout=1):
        return transport.invoke(dict(base_url=url,timeout_seconds=timeout),'/responses',b'{"model":"fixture"}',secret)

    def test_both_protocols_through_persistent_runner(self):
        with tempfile.TemporaryDirectory() as directory,server() as (url,calls):
            cfg=configuration(url);runtime=ModelRuntime.create(Path(directory)/'state.sqlite',encoded(cfg))
            for index,name in enumerate(('openai','local')):
                if index:
                    cfg['active_profile']=name;runtime.configure('switch',1,encoded(cfg))
                runtime.approve('approve-'+name,name,account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
                request=Generation('example',name,'answer','Synthetic',('openai','local'),32,time.time_ns()//1000000+10000)
                runtime.submit(request);result=runtime.run('example',name)
                self.assertTrue(result.call.terminal);self.assertEqual(result.call.outcome,'ok')
                self.assertEqual(runtime.read_result('example',name),'{"ok":true}')
                runtime=ModelRuntime(Path(directory)/'state.sqlite');runtime.run('example',name)
            self.assertEqual([c[0] for c in calls],['/v1/responses','/v1/chat/completions'])
            self.assertEqual(runtime.budget().issued_calls,2)
            self.assertEqual(runtime.budget().input_bytes,sum(len(c[1]) for c in calls))

    def test_deadline_redirect_short_capture_and_bound(self):
        for mode in ('redirect','short','stall','oversize','exact'):
            with self.subTest(mode=mode),server(mode) as (url,calls):
                start=time.monotonic();meta,raw=self.invoke(url)
                self.assertLess(time.monotonic()-start,3)
                self.assertEqual(len(calls),1)
                if mode=='redirect':self.assertEqual(meta['status'],302)
                elif mode=='exact':self.assertEqual((len(raw),meta['complete']),(262144,True))
                else:self.assertFalse(meta['complete'])
                self.assertLessEqual(len(raw),262144)
                self.assertEqual(protocol.interpret('openai_responses',meta['status'],raw,meta['complete'])['remote_state'],'unknown')

    def test_header_and_chunk_framing_rejects_ambiguous_eof(self):
        raw=response()
        valid=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'+format(len(raw),'x').encode()+b'\r\n'+raw+b'\r\n0\r\n\r\n'
        messages=[valid,valid[:-2],valid.replace(b'\r\n0',b'XX0'),valid.replace(b'Transfer-Encoding:',b'Bad Header:\r\nTransfer-Encoding:'),
                  valid.replace(b'\r\n\r\n',b'\r\nContent-Length: 2\r\n\r\n',1)]
        for index,message in enumerate(messages):
            with self.subTest(index=index),server(message=message) as (url,calls):
                meta,captured=self.invoke(url)
                self.assertEqual(meta['complete'],index==0)
                self.assertEqual(len(calls),1)
                if index==0:self.assertEqual(captured,raw)

    def test_independent_processes_compete_for_one_original_send(self):
        import os
        import subprocess
        import sys
        import infra_model_runtime
        package=str(Path(infra_model_runtime.__file__).resolve().parents[1])
        env=dict(os.environ,PYTHONPATH=package,PYTHONDONTWRITEBYTECODE='1')
        with tempfile.TemporaryDirectory() as directory,server() as (url,calls):
            cfg=configuration(url);path=Path(directory)/'race.sqlite'
            runtime=ModelRuntime.create(path,encoded(cfg))
            runtime.approve('approve','openai',account='synthetic',service_revision='fixture',capability_contract=CONTRACT)
            runtime.submit(Generation('example','race','answer','Synthetic',('openai',),32,time.time_ns()//1000000+10000))
            worker=str(Path(__file__).with_name('r2_race_worker.py'))
            processes=[subprocess.Popen([sys.executable,'-B',worker,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,cwd=directory) for _ in range(2)]
            try:
                outputs=[p.communicate(timeout=10) for p in processes]
            finally:
                for p in processes:
                    if p.poll() is None:p.kill();p.communicate()
            for p,(out,err) in zip(processes,outputs):
                self.assertEqual(p.returncode,0,err.decode())
                self.assertTrue(json.loads(out)['issued'])
            runtime=ModelRuntime(path);runtime.recover('example','race')
            self.assertTrue(runtime.status('example','race').call.terminal)
            self.assertEqual(len(calls),1)
            self.assertEqual(runtime.budget().issued_calls,1)
            self.assertEqual(runtime.budget().input_bytes,len(calls[0][1]))

    def test_secret_echo_suppressed_before_persistence(self):
        with server('echo') as (url,calls):
            meta,raw=self.invoke(url,'synthetic-secret')
            self.assertEqual(raw,b'');self.assertFalse(meta['complete'])
            self.assertEqual(calls[0][2]['Authorization'],'Bearer synthetic-secret')


if __name__=='__main__':unittest.main()
