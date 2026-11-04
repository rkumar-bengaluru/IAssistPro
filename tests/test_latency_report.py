import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _report_module():
    spec = importlib.util.spec_from_file_location("latency_report", ROOT / "scripts" / "latency_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(tmp_path, gaps, splits=0):
    p = tmp_path / "live.json"
    p.write_text(json.dumps({"gaps": gaps, "result_latency": {"n": len(gaps)}, "usage": [], "cost": 0.001,
                             "end_reason": "completed", "splits": splits}))
    return str(p)


def test_report_bounds_are_4500_and_5500(tmp_path, capsys):
    m = _report_module()
    assert (m.MEDIAN_MAX_MS, m.P90_MAX_MS, m.MIN_GAPS) == (4500, 5500, 10)
    assert m.report(_run(tmp_path, [4400] * 9 + [5500])) is True             # AT3 bounds, inclusive
    assert m.report(_run(tmp_path, [4600] * 10)) is False                    # median over 4.5 s
    assert m.report(_run(tmp_path, [4000] * 8 + [5600] * 2)) is False        # p90 over 5.5 s
    assert m.report(_run(tmp_path, [3000] * 10, splits=1)) is False          # a split answer fails the run
    assert "splits=1" in capsys.readouterr().out
