"""Protocol evidence and exact wire/body/unit boundaries, independent expected payloads."""
import copy
import json
import unittest
from infra_model_runtime._boundary import InputError, encoded
from infra_model_runtime import configuration as config
from infra_model_runtime import protocol
from r2_fixtures import configuration
from r2_fixtures import response


class ModelProtocolTests(unittest.TestCase):
    def setUp(self):
        self.profiles=configuration()['profiles']
        self.contract=dict(chat_output_parameter='max_tokens',context_tokens=1000000,input_tokens_per_byte=2,evidence_ref='fixture')

    def test_exact_wire_modes_and_no_implicit_remote_state(self):
        for name in ('openai','local'):
            p=self.profiles[name];path,raw=protocol.prepare(p,'Question: "你好"',32,self.contract)
            body=json.loads(raw)
            if name=='openai':
                self.assertEqual(path,'/responses')
                self.assertEqual(body,dict(model=p['model'],instructions=protocol.SYSTEM,input='Question: "你好"',max_output_tokens=32,
                    stream=False,store=False,background=False,tools=[],truncation='disabled',text={'format':{'type':'json_object'}}))
            else:
                self.assertEqual(path,'/chat/completions')
                self.assertEqual(body,dict(model=p['model'],messages=[dict(role='system',content=protocol.SYSTEM),dict(role='user',content='Question: "你好"')],
                    stream=False,n=1,response_format={'type':'json_object'},max_tokens=32))
                contract=dict(self.contract,chat_output_parameter='max_completion_tokens')
                other=json.loads(protocol.prepare(p,'x',32,contract)[1])
                self.assertEqual(other['max_completion_tokens'],32);self.assertNotIn('max_tokens',other)

    def test_complete_wire_and_context_exact_limits(self):
        p=copy.deepcopy(self.profiles['local'])
        raw=protocol.prepare(p,'x',32,self.contract)[1]
        p['max_input_bytes']=len(raw)
        self.assertEqual(protocol.prepare(p,'x',32,self.contract)[1],raw)
        p['max_input_bytes']-=1
        with self.assertRaises(InputError):protocol.prepare(p,'x',32,self.contract)
        p['max_input_bytes']=65536
        exact=dict(self.contract,context_tokens=len(raw)*2+32)
        protocol.prepare(p,'x',32,exact)
        with self.assertRaises(InputError):protocol.prepare(p,'x',32,dict(exact,context_tokens=exact['context_tokens']-1))

    def test_nonterminal_and_incomplete_capture_cannot_release_remote_slot(self):
        for adapter in ('openai_responses','openai_chat'):
            raw=response(adapter)
            for http_status,complete in ((200,False),(401,True),(429,True),(503,True),(302,True)):
                with self.subTest(adapter=adapter,status=http_status,complete=complete):
                    self.assertEqual(protocol.interpret(adapter,http_status,raw,complete)['remote_state'],'unknown')
            for raw in (b'{',b'{"id":"a","id":"b"}',b'\xff',b'['*100+b']'*100):
                self.assertEqual(protocol.interpret(adapter,200,raw,True)['remote_state'],'unknown')
        self.assertEqual(protocol.interpret('openai_responses',200,response(status='in_progress'),True)['remote_state'],'unknown')

    def test_terminal_refusal_truncation_and_invalid_output_are_failures(self):
        for adapter in ('openai_responses','openai_chat'):
            for text in ('not JSON','[]','{"duplicate":1,"duplicate":2}','{"x":NaN}','x'*65537):
                with self.subTest(adapter=adapter,text=text[:20]):
                    found=protocol.interpret(adapter,200,response(adapter,text=text),True)
                    self.assertEqual(found['remote_state'],'terminal');self.assertEqual(found['code'],'OUTPUT_INVALID')
                    self.assertIsNone(found['output_text'])
        raw=json.loads(response('openai_chat'));raw['choices'][0]['finish_reason']='length'
        found=protocol.interpret('openai_chat',200,encoded(raw),True)
        self.assertEqual((found['remote_state'],found['code']),('terminal','OUTPUT_INVALID'))

    def test_usage_units_types_missing_and_invalid_higher_output(self):
        for adapter,fields in (('openai_responses',('input_tokens','output_tokens')),('openai_chat',('prompt_tokens','completion_tokens'))):
            u={fields[0]:4096,fields[1]:32,'total_tokens':4128}
            found=protocol.usage(u,adapter)
            self.assertEqual(found['status'],'known');self.assertEqual(found['output_tokens'],32)
            for value in (True,-1,1.0,2**63,'32'):
                damaged=dict(u);damaged[fields[1]]=value
                self.assertEqual(protocol.usage(damaged,adapter)['status'],'invalid')
            damaged=dict(u,total_tokens=0);damaged[fields[1]]=2049
            found=protocol.usage(damaged,adapter)
            self.assertEqual(found['status'],'invalid');self.assertEqual(found['output_tokens'],2049)
            missing=protocol.usage(None,adapter)
            self.assertEqual(missing['status'],'missing');self.assertIsNone(missing['output_tokens'])

    def test_malformed_authority_is_rejected_before_transport(self):
        for value in ('http://[::1]evil/v1','https://example.com:/v1','https://[::1]:/v1','https://example.com:abc/v1'):
            with self.subTest(value=value),self.assertRaises(InputError):config.endpoint(value)
        for value in ('http://[::1]:80/v1','http://127.0.0.1:123/v1','https://example.com/v1'):
            config.endpoint(value)


if __name__=='__main__':unittest.main()
