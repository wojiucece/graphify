"""A2 脱敏：L1 表驱动全命中 + 误伤样例集 + 出口统一 + 性能门."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graphify.serve import _redact

HITS = {  # kind -> (正例, 必须不匹配的边界负例)
    "openai": ("sk-" + "aA0" * 11 + "x", "sk-short"),
    "anthropic": ("sk-ant-api03-" + "aB1" * 27 + "z", "sk-ant-api03-short"),
    "gemini": ("AIza" + "0Az_" * 8 + "abc", "AIza-short"),  # 35 位（真实 key 39 字符总长，模式 {35}）
    "github": ("ghp_" + "aB3" * 12 + "x", "ghp_no"),
    "aws": ("AKIA" + "IOSFODNN7EXAMPLE"[:16], "AKIA123"),  # 16 位 [0-9A-Z]
}
def test_l1_hits_and_boundaries():
    for kind, (good, bad) in HITS.items():
        assert f"[REDACTED:{kind}]" in _redact(f"key={good}"), kind
        assert good not in _redact(f"key={good}")
    for kind, (_, bad) in HITS.items():
        assert bad in _redact(f"key={bad}") or "[REDACTED" not in _redact(f"key={bad}"), kind

def test_false_positive_guard():
    """误伤样例集：普通 base64 / URL / 长代码 token 不误报（R16）."""
    for s in ["dGhpcyBpcyBwbGFpbiBiYXNlNjQ=", "https://example.com/a/b?x=1",
              "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"]:  # JWT 属 L1 命中，前两条必须不误报
        if s.startswith(("http", "dGhp")):
            assert "[REDACTED" not in _redact(s), s

def test_jwt_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert "[REDACTED:jwt]" in _redact(f"token {jwt}")

def test_perf_gate_10kb():
    text = ("filler " * 1700)   # ~10KB
    t0 = time.perf_counter(); _redact(text); dt = time.perf_counter() - t0
    assert dt < 0.010, f"10KB 脱敏 {dt*1000:.1f}ms 超 10ms 门"
