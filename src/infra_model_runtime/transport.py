"""Bounded subprocess HTTP transport: deadline also covers DNS and slow peers."""
import http.client
import json
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import configuration as config
from .protocol import MAX_RESPONSE
from ._boundary import encoded

HEADER_LIMIT = 16384
FRAMING_LIMIT = 65536
_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_QUOTED = rb'"(?:[\t \x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t \x21-\x7e\x80-\xff])*"'
_CHUNK_LINE = re.compile(rb'([0-9A-Fa-f]+)(?:[ \t]*;[ \t]*'+_TOKEN+
    rb'(?:[ \t]*=[ \t]*(?:'+_TOKEN+rb'|'+_QUOTED+rb'))?)*\r\n')
_STATUS_LINE = re.compile(rb'HTTP/1\.[01] ([0-9]{3})(?: [\t \x21-\x7e\x80-\xff]*)?\r\n')


class _HeadReader:
    """Validate wire head lines before the email-based stdlib parser sees them.

    Malformed fields can make that parser silently discard later framing fields.
    Count the complete raw head, including any skipped 100 Continue responses.
    Only used during HTTPResponse.begin; body reads retain the original stream.
    """
    def __init__(self, stream):
        self.stream=stream
        self.total=0
        self.status_line=True
        self.interim=False

    def __getattr__(self, name):
        return getattr(self.stream,name)

    def readline(self, limit=-1):
        maximum=HEADER_LIMIT-self.total+1
        line=self.stream.readline(maximum if limit<0 else min(limit,maximum))
        self.total+=len(line)
        if self.total>HEADER_LIMIT or not line.endswith(b'\r\n'):
            raise ValueError('incomplete or oversized response head')
        if self.status_line:
            match=_STATUS_LINE.fullmatch(line)
            if match is None:raise ValueError('invalid response status line')
            self.interim=match[1]==b'100'
            self.status_line=False
        elif line==b'\r\n':
            self.status_line=self.interim
        else:
            name,colon,value=line[:-2].partition(b':')
            if (not colon or re.fullmatch(_TOKEN,name) is None
                or (self.interim and name.lower() in (b'content-length',b'transfer-encoding'))
                or any(byte<32 and byte!=9 or byte==127 for byte in value)):
                raise ValueError('invalid response header field')
        return line


class _Response(http.client.HTTPResponse):
    def begin(self):
        if self.headers is not None:return
        reader=_HeadReader(self.fp)
        self.fp=reader
        try:
            super().begin()
        finally:
            if self.fp is reader:self.fp=reader.stream


class _ChunkedBody:
    """RFC 9112 framing with finite metadata; EOF cannot stand in for CRLF.

    http.client accepts some malformed chunk delimiters and missing trailer
    termination. Use the response's buffered body stream to verify these facts
    before reporting a complete model response; never merge trailers into headers.
    """
    def __init__(self, stream):
        self.stream=stream
        self.left=0
        self.separator=False
        self.done=False
        self.metadata_bytes=0

    def _account(self, count):
        self.metadata_bytes+=count
        if self.metadata_bytes>FRAMING_LIMIT:
            raise ValueError('chunk metadata limit')

    def _line(self):
        line=self.stream.readline(min(HEADER_LIMIT+1,FRAMING_LIMIT-self.metadata_bytes+1))
        self._account(len(line))
        if len(line)>HEADER_LIMIT or not line.endswith(b'\r\n'):
            raise ValueError('incomplete or oversized chunk metadata line')
        return line

    def _trailers(self):
        total=0
        while True:
            line=self._line();total+=len(line)
            if total>HEADER_LIMIT:
                raise ValueError('trailer limit')
            if line==b'\r\n':return
            name, colon, value=line[:-2].partition(b':')
            if (not colon or re.fullmatch(_TOKEN,name) is None
                or name.lower() in (b'content-length',b'transfer-encoding',b'content-encoding')
                or any(byte<32 and byte!=9 or byte==127 for byte in value)):
                raise ValueError('unsupported trailer field')

    def read1(self, maximum):
        if self.done:return b''
        if self.left==0:
            if self.separator:
                separator=self.stream.read(2);self._account(len(separator))
                if separator!=b'\r\n':raise ValueError('invalid chunk data delimiter')
            match=_CHUNK_LINE.fullmatch(self._line())
            if match is None:raise ValueError('invalid chunk size or extension')
            self.left=int(match[1],16)
            if self.left==0:
                self._trailers();self.done=True
                return b''
            self.separator=True
        block=self.stream.read1(min(maximum,self.left))
        if not block:raise ValueError('incomplete chunk data')
        self.left-=len(block)
        return block


def _stop(process):
    # Cleanup cannot replace a timeout or caller interruption, nor wait forever.
    try:
        process.kill()
    except BaseException:
        pass
    try:
        process.communicate(timeout=1)
    except BaseException:
        pass


# Match only JSON escape spellings. Credentials are printable ASCII, so non-ASCII
# escapes cannot contribute a credential byte and remain untouched. Each pass
# shrinks or preserves the bounded input; never recursively parse peer JSON.
_JSON_ESCAPE = re.compile(rb'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
_JSON_SIMPLE = {b'"':b'"', b'\\':b'\\', b'/':b'/', b'b':b'\b',
                b'f':b'\f', b'n':b'\n', b'r':b'\r', b't':b'\t'}


def _ascii_json_escape(match):
    if match[1] is not None:
        value = int(match[1], 16)
        return bytes((value,)) if value < 128 else match[0]
    return _JSON_SIMPLE[match[2]]


def _echoes_secret(raw, secret):
    token = secret.encode('ascii')
    if token in raw:
        return True
    # Inspect the provider envelope and one embedded JSON encoding even when
    # framing, UTF-8, duplicate keys, or JSON limits make full parsing impossible.
    # Scan a private copy only; non-secret captures retain their exact wire bytes.
    for _ in range(2):
        decoded = _JSON_ESCAPE.sub(_ascii_json_escape, raw)
        if token in decoded:
            return True
        if decoded == raw:
            break
        raw = decoded
    return False


def _child():
    import base64
    request = json.loads(sys.stdin.buffer.read(131073))
    raw = base64.b64decode(request['wire'], validate=True)
    uri = urlsplit(request['url'])
    deadline = time.monotonic()+request['timeout']
    connection = (http.client.HTTPSConnection if uri.scheme == 'https' else http.client.HTTPConnection)(
        uri.hostname, uri.port, timeout=request['timeout'])
    connection.response_class=_Response
    captured = b''
    status, complete, sent = None, False, 'not_sent'
    try:
        connection.connect()  # Parent deadline also covers blocking DNS.
        headers = {'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity','Connection':'close'}
        if request['secret'] is not None:
            headers['Authorization'] = 'Bearer '+request['secret']
        sent = 'maybe_sent'
        connection.request('POST', uri.path or '/', body=raw, headers=headers)
        response = connection.getresponse()
        sent, status = 'response_received', response.status
        pairs = response.getheaders()
        if sum(len(k)+len(v)+4 for k,v in pairs) > HEADER_LIMIT:
            raise ValueError('header limit')
        lengths = [v.strip(' \t') for v in response.headers.get_all('Content-Length', [])]
        encodings = response.headers.get_all('Content-Encoding', [])
        transfer = response.headers.get_all('Transfer-Encoding', [])
        if (len(lengths)>1 or (lengths and transfer) or len(transfer)>1
                or (transfer and response.version==10)
                or (transfer and transfer[0].strip(' \t').lower() != 'chunked')
                or (encodings and (len(encodings)!=1 or encodings[0].strip(' \t').lower()!='identity'))
                or (lengths and (not lengths[0].isascii() or not lengths[0].isdigit()))):
            raise ValueError('unsupported response framing')
        body=_ChunkedBody(response.fp) if transfer else response
        # Read one byte beyond the limit to distinguish exact EOF from truncation.
        while len(captured) <= MAX_RESPONSE:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            block = body.read1(min(8192, MAX_RESPONSE+1-len(captured)))
            if not block:
                complete = not lengths or len(captured) == int(lengths[0])
                break
            captured += block
        if len(captured)>MAX_RESPONSE:
            captured, complete = captured[:MAX_RESPONSE], False
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    metadata = encoded(dict(status=status, complete=complete, sent=sent))+b'\n'
    sys.stdout.buffer.write(metadata+captured)
    sys.stdout.buffer.flush()


def invoke(profile, suffix, wire, secret):
    """Exactly one POST. No redirects, ambient proxy, automatic retry or fallback."""
    import base64
    config.endpoint(profile['base_url'])
    payload = encoded(dict(url=profile['base_url'].rstrip('/')+suffix, wire=base64.b64encode(wire).decode(),
                           secret=secret, timeout=profile['timeout_seconds']))
    # Bind the child to this exact package, including unpacked-wheel validation.
    from pathlib import Path
    package = str(Path(__file__).resolve().parents[1])
    # -m puts the caller's cwd ahead of PYTHONPATH. Isolate Python startup and
    # explicitly load the parent's package; credentials still travel only on stdin.
    bootstrap = ('import sys; sys.path.insert(0, sys.argv[1]); '
                 'from infra_model_runtime.transport import _child; _child()')
    deadline = time.monotonic()+profile['timeout_seconds']
    process = subprocess.Popen([sys.executable,'-I','-B','-c',bootstrap,package], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    output = b''
    try:
        # communicate enforces an absolute wall deadline, including sending input to the child.
        output, _ = process.communicate(payload, timeout=max(0.001, deadline-time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop(process)
        return dict(status=None, complete=False, sent='maybe_sent'), b''
    except BaseException:
        _stop(process)
        raise
    if process.returncode != 0 or len(output)>MAX_RESPONSE+512 or b'\n' not in output:
        return dict(status=None, complete=False, sent='maybe_sent'), b''
    header, raw = output.split(b'\n',1)
    try:
        meta = json.loads(header)
        if set(meta) != {'status','complete','sent'} or type(meta['complete']) is not bool or meta['sent'] not in ('not_sent','maybe_sent','response_received'):
            raise ValueError()
        if meta['status'] is not None and (type(meta['status']) is not int or not 100 <= meta['status'] <= 999):
            raise ValueError()
    except (ValueError, TypeError):
        return dict(status=None, complete=False, sent='maybe_sent'), b''
    if secret is not None and _echoes_secret(raw,secret):
        # A peer echo of the credential must not enter persistent evidence or user output.
        return dict(status=meta['status'], complete=False, sent=meta['sent']), b''
    return meta, raw


if __name__ == '__main__':
    _child()
