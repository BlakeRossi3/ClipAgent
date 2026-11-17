"""quick.call falls back to the Claude plan when the API credit balance is gone."""

from assistant import quick


class _NoCredit:
    fake = False

    def create(self, req):
        raise RuntimeError("Error code: 400 - Your credit balance is too low to access the Anthropic API.")


class _Other:
    fake = False

    def create(self, req):
        raise RuntimeError("boom")


def _req():
    return object()


def test_out_of_credit_falls_back_to_plan(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(quick.ClaudeCodeQuick, "create", lambda self, req: sentinel)
    assert quick.call(_NoCredit(), _req()) is sentinel


def test_other_errors_still_raise():
    try:
        quick.call(_Other(), _req())
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected error")
