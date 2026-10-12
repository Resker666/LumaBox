import subprocess
import sys
import json


def test_authorization_is_required(tmp_path):
    process = subprocess.run([sys.executable, "-m", "lumabox", "https://v.douyin.com/a/", "--output", str(tmp_path)], capture_output=True, text=True)
    assert process.returncode != 0
    assert "--authorized" in process.stderr
    assert list(tmp_path.iterdir()) == []


def test_invalid_input_returns_json_and_nonzero_exit(tmp_path):
    process = subprocess.run([sys.executable, "-m", "lumabox", "bad input", "--authorized", "--output", str(tmp_path)], capture_output=True, text=True)
    assert process.returncode == 1
    assert json.loads(process.stdout)["error"] == "INVALID_INPUT"
