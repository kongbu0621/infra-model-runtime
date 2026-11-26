"""Prove DNS was entered by the exact child package before deadline cleanup."""
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from infra_model_runtime import transport


class DNSDeadlineTests(unittest.TestCase):
    def test_blocked_dns_is_entered_by_exact_package_then_child_is_reaped(self):
        actual=subprocess.Popen;children=[]
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'dns-entered'
            def start(args,**kwargs):
                args=list(args);index=args.index('-c')+1
                # Retain the real isolated bootstrap and its exact package path.
                # Only replace DNS with a controlled block; do not execute network I/O.
                code=("import socket, time, sys\nfrom pathlib import Path\n"
                      "def blocked(*args, **kwargs):\n"
                      f"    Path({str(marker)!r}).write_text(str(Path(sys.modules[_child.__module__].__file__).resolve()))\n"
                      "    time.sleep(10)\n"
                      "socket.getaddrinfo=blocked\n")
                args[index]=code+args[index]
                child=actual(args,**kwargs);children.append(child);return child
            started=time.monotonic()
            try:
                with patch.object(transport.subprocess,'Popen',side_effect=start):
                    meta,raw=transport.invoke(dict(base_url='http://localhost:1/v1',timeout_seconds=1),'/responses',b'{}',None)
                elapsed=time.monotonic()-started
                self.assertEqual(len(children),1)
                self.assertTrue(marker.exists(),'child did not enter the injected DNS boundary')
                self.assertEqual(marker.read_text(),str(Path(transport.__file__).resolve()))
                self.assertGreaterEqual(elapsed,0.75)
                self.assertLess(elapsed,3)
                self.assertIsNotNone(children[0].poll(),'deadline left an unreaped child')
                self.assertEqual(meta,dict(status=None,complete=False,sent='maybe_sent'))
                self.assertEqual(raw,b'')
            finally:
                for child in children:
                    if child.poll() is None:child.kill();child.communicate(timeout=2)


if __name__=='__main__':unittest.main()
