"""CLI argument handling."""
from postradio.cli import parser


def _parse(argv):
    a = parser().parse_args(argv)
    return {k: getattr(a, k, None) for k in ("json", "no_token", "offline", "config", "artist")}


def test_global_flags_work_before_or_after_the_subcommand():
    before = _parse(["--json", "--no-token", "--config", "c.yaml", "radio", "--artist", "X"])
    after = _parse(["radio", "--artist", "X", "--json", "--no-token", "--config", "c.yaml"])
    assert before == after == {"json": True, "no_token": True, "offline": None, "config": "c.yaml", "artist": "X"}


def test_unset_global_flags_are_absent_not_false_overrides():
    a = parser().parse_args(["--json", "radio", "--artist", "X"])
    assert a.json is True  # a subparser default must not clobber the value given before the subcommand
