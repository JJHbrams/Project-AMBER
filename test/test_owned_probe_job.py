"""Windows timeout containment of an owned probe, never user processes."""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows Job containment')
class OwnedProbeJobTests(unittest.TestCase):
    def test_killing_wrapper_also_ends_owned_descendant(self):
        try:
            import psutil
        except ImportError:
            self.skipTest('Run with the installed Engram Python environment (psutil required)')
        scripts = Path(__file__).resolve().parents[1] / 'scripts/dev'
        code = ('import subprocess,sys,time; '
                f'sys.path.insert(0,{str(scripts)!r}); '
                'from owned_probe_job import contain_current_probe; contain_current_probe(); '
                'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(45)"]); '
                'print(child.pid,flush=True); time.sleep(45)')
        wrapper = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
        child = None
        try:
            child = psutil.Process(int(wrapper.stdout.readline().strip()))
            self.assertTrue(child.is_running())
            wrapper.terminate()
            wrapper.wait(timeout=5)
            deadline = time.monotonic() + 5
            while child.is_running() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(child.is_running())
        finally:
            if wrapper.poll() is None:
                wrapper.terminate()
                wrapper.wait(timeout=5)
            if child is not None and child.is_running():
                child.terminate()  # Exact captured PID and creation time only.
                child.wait(timeout=5)
            wrapper.stdout.close()


if __name__ == '__main__':
    unittest.main()
