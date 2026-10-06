"""Start the real entry point (fake player) and check it comes up without tracebacks."""

import socket
import subprocess
import sys
import time


def _free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_projektor_run_starts(tmp_path):
    data = tmp_path / "data"
    (data / "media").mkdir(parents=True)
    cfg = tmp_path / "p.toml"
    cfg.write_text(f"""
[paths]
data_dir = "{data}"
net_status = "{tmp_path}/net.json"
tunnel_status = "{tmp_path}/tunnel.json"
[osc]
port = {_free_port(socket.SOCK_DGRAM)}
[web]
port = {_free_port()}
[lora]
enabled = false
[beamer]
enabled = false
""")
    p = subprocess.Popen([sys.executable, "-m", "projektor", "run", "--player", "fake", "--config", str(cfg)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(6)
        assert p.poll() is None, "process exited early"
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0]
    assert "web UI on" in out, out
    assert "Traceback" not in out, out
