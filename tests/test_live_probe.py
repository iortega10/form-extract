from __future__ import annotations

import contextlib
import http.server
import json
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tools"))

import live_probe as probe  # noqa: E402

FIELD_LABEL = "ZZTOP-SECRET-LABEL"
OK_FIELDS = (
    '{"fields": [{"label": "' + FIELD_LABEL + '", "control_type": "text", '
    '"options": [], "answer": ["v"], "annotations": []}]}'
)
TRUNCATED_FIELDS = '{"fields": [{"label": "' + FIELD_LABEL + '", "control_type": "text"'


def _openai_body(text: str, finish: str) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        }
    ).encode()


def _gemini_body(text: str, finish: str) -> bytes:
    return json.dumps(
        {
            "candidates": [
                {"content": {"parts": [{"text": text}]}, "finishReason": finish}
            ],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
                "thoughtsTokenCount": 3,
            },
        }
    ).encode()


@contextlib.contextmanager
def _fake_server(*, ok_body: bytes, trunc_body: bytes, key: str):
    state = {
        "requests": [],
        "counts": {},
        "key": key,
        "ok_body": ok_body,
        "trunc_body": trunc_body,
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D102 - silence the test server
            pass

        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            state["requests"].append(
                {
                    "path": self.path,
                    "requestline": self.requestline,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            route = self.path.split("?", 1)[0].strip("/").split("/")[0]
            state["counts"][route] = state["counts"].get(route, 0) + 1
            if route == "e401":
                payload = (
                    '{"error": {"message": "invalid key: ' + key + '"}}'
                ).encode()
                self._send(401, payload)
            elif route == "e400":
                self._send(400, b'{"error": {"message": "bad request"}}')
            elif route == "e429":
                self._send(429, b'{"error": {"message": "slow down"}}')
            elif route == "slow":
                time.sleep(0.3)
                self._send(200, ok_body)
            elif route == "trunc":
                self._send(200, trunc_body)
            else:
                self._send(200, ok_body)

        def _send(self, code: int, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait_idle(before: int) -> int:
    deadline = time.time() + 3
    while threading.active_count() != before and time.time() < deadline:
        time.sleep(0.02)
    return threading.active_count()


def _run(argv: list[str]) -> int:
    try:
        return probe.main(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1


def _make_workbook(path: Path, sheets: tuple[str, ...] = ("Alpha Log",)) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    first = wb.active
    first.title = sheets[0]
    for name in sheets:
        ws = first if name == sheets[0] else wb.create_sheet(name)
        ws["A1"] = name
        ws["A2"] = "Priority"
        ws["B2"] = "Low"
        ws["C2"] = "X"
        ws["D2"] = "High"
    wb.save(path)
    wb.close()
    return path


def _assert_scalar(value) -> None:
    if value is None or isinstance(value, (int, float, bool)):
        return
    assert isinstance(value, str) and value in probe.ALLOWED_STRING_VALUES, value


def _last_line(out: str) -> dict:
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines
    return json.loads(lines[-1])


def test_stdout_is_numbers_only_and_leaks_no_text(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx")
    before = threading.active_count()
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
            ]
        )
        out, err = capsys.readouterr()

    assert err == ""
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    for value in data.values():
        _assert_scalar(value)
    assert FIELD_LABEL not in out
    assert "control_type" not in out
    # row coverage (T4): the denominator is anchors + GRID-region units, and
    # the old ad-hoc key is gone
    assert "anchors_consumed" not in data
    assert data["units_total"] == data["anchors"] + data["grid_units"]
    assert 0 <= data["units_consumed"] <= data["units_total"]
    assert "annotations" not in out
    assert data["chunks_run"] == 1
    assert data["fields"] == 1
    assert data["prompt_tokens"] == 11
    assert data["output_tokens"] == 7
    assert data["reasoning_tokens"] == 3
    assert data["status_class"] == "complete"
    assert data["http_error_class"] == "none"
    assert _wait_idle(before) == before


def test_key_never_appears_even_when_the_body_echoes_it(tmp_path, capsys, monkeypatch):
    key = "test-key-not-real"
    monkeypatch.setenv("PROBE_TEST_KEY", key)
    path = _make_workbook(tmp_path / "wb.xlsx")
    before = threading.active_count()
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=key
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/e401",
                "--env-var", "PROBE_TEST_KEY",
            ]
        )
        out, err = capsys.readouterr()

    assert code == 0
    assert key not in out
    assert key not in err
    data = _last_line(out)
    assert data["http_error_class"] == "4xx"
    assert _wait_idle(before) == before


def test_redaction_scrubs_message_and_traceback():
    key = "test-key-not-real"
    secrets = {"test-key-not-real", "Bearer test-key-not-real"}
    exc = RuntimeError(f"call failed with {key}")
    name, message, traceback_text = probe.render_error(exc, secrets)
    assert name == "RuntimeError"
    assert key not in message
    assert key not in traceback_text
    assert "<redacted>" in message


def test_unexpected_error_is_reported_redacted(tmp_path, capsys, monkeypatch):
    key = "test-key-not-real"
    monkeypatch.setenv("PROBE_TEST_KEY", key)
    path = _make_workbook(tmp_path / "wb.xlsx")

    def boom(*args, **kwargs):
        raise RuntimeError(f"install failed for {key}")

    monkeypatch.setattr(probe, "ingest", boom)
    code = _run(
        [
            str(path),
            "--provider", "openai",
            "--model", "test-model",
            "--base-url", "http://127.0.0.1:1/ok",
            "--env-var", "PROBE_TEST_KEY",
        ]
    )
    out, err = capsys.readouterr()

    assert code == 1
    assert key not in out
    assert key not in err
    assert "RuntimeError" in err


def test_4xx_is_classified_and_costs_one_request(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx")
    before = threading.active_count()
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/e400",
                "--env-var", "",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    assert state["counts"].get("e400") == 1
    data = _last_line(out)
    assert data["http_error_class"] == "4xx"
    assert data["status_class"] == "failed"
    assert _wait_idle(before) == before


def test_length_cap_and_truncated_are_reported(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx")
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"),
        trunc_body=_openai_body(TRUNCATED_FIELDS, "length"),
        key="",
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/trunc",
                "--env-var", "",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    data = _last_line(out)
    assert data["hit_length_cap"] is True
    assert data["truncated"] is True
    assert data["finish_reason_class"] == "length"
    assert data["fields"] == 0
    assert data["status_class"] == "partial"


def test_gemini_key_is_only_in_the_header(tmp_path, capsys, monkeypatch):
    key = "test-key-not-real"
    monkeypatch.setenv("PROBE_TEST_KEY", key)
    path = _make_workbook(tmp_path / "wb.xlsx")
    before = threading.active_count()
    with _fake_server(
        ok_body=_gemini_body(OK_FIELDS, "STOP"), trunc_body=b"", key=key
    ) as (_server, base, state):
        code = _run(
            [
                str(path),
                "--provider", "gemini",
                "--model", "gemini-test",
                "--base-url", base + "/ok",
                "--env-var", "PROBE_TEST_KEY",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    assert state["requests"]
    for request in state["requests"]:
        assert key not in request["path"]
        assert key not in request["requestline"]
        assert "key=" not in request["path"]
        assert request["headers"].get("x-goog-api-key") == key
        assert "authorization" not in request["headers"]
    assert _last_line(out)["fields"] == 1
    assert _wait_idle(before) == before


def test_openai_key_is_in_the_authorization_header(tmp_path, capsys, monkeypatch):
    key = "test-key-not-real"
    monkeypatch.setenv("PROBE_TEST_KEY", key)
    path = _make_workbook(tmp_path / "wb.xlsx")
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=key
    ) as (_server, base, state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "PROBE_TEST_KEY",
            ]
        )
        capsys.readouterr()

    assert code == 0
    for request in state["requests"]:
        assert key not in request["path"]
        assert request["headers"].get("authorization") == "Bearer " + key


def test_missing_env_var_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PROBE_UNSET_KEY_XYZ", raising=False)
    path = _make_workbook(tmp_path / "wb.xlsx")
    code = _run(
        [
            str(path),
            "--provider", "openai",
            "--model", "test-model",
            "--env-var", "PROBE_UNSET_KEY_XYZ",
        ]
    )
    out, err = capsys.readouterr()

    assert code == 2
    assert out == ""
    assert "key variable PROBE_UNSET_KEY_XYZ is not set" in err


def test_tab_takes_an_index_and_rejects_a_name(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx", sheets=("Alpha Log", "Beta Log"))
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
                "--tab", "1",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    assert _last_line(out)["chunks_run"] == 1

    code_name = _run(
        [
            str(path),
            "--provider", "openai",
            "--model", "test-model",
            "--base-url", base + "/ok",
            "--env-var", "",
            "--tab", "Alpha Log",
        ]
    )
    assert code_name == 2

    code_range = _run(
        [
            str(path),
            "--provider", "openai",
            "--model", "test-model",
            "--base-url", base + "/ok",
            "--env-var", "",
            "--tab", "9",
        ]
    )
    assert code_range == 1


def test_workers_match_serial_counts(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx", sheets=("Alpha Log", "Beta Log"))
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code1 = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
                "--workers", "1",
            ]
        )
        out1, _err1 = capsys.readouterr()
        code2 = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
                "--workers", "4",
            ]
        )
        out2, _err2 = capsys.readouterr()

    assert code1 == 0 and code2 == 0
    serial = json.loads(out1.strip())
    pooled = json.loads(out2.strip())
    assert serial["chunks_run"] == 2 and pooled["chunks_run"] == 2
    for key, value in serial.items():
        if key in ("seconds", "tokens_per_second"):
            continue
        assert pooled[key] == value, key


def test_tiny_reports_floor_seconds(tmp_path, capsys):
    with _fake_server(
        ok_body=_openai_body('{"ok": true}', "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code = _run(
            [
                "--tiny",
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    data = _last_line(out)
    assert isinstance(data["floor_seconds"], float)
    assert data["output_tokens"] == 7


def test_path_is_required_without_tiny():
    assert _run(["--provider", "openai", "--model", "test-model"]) == 2


def test_repeat_prints_one_aggregated_object(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx")
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
                "--repeat", "3",
            ]
        )
        out, _err = capsys.readouterr()

    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    for value in data.values():
        _assert_scalar(value)
    assert data["repeat"] == 3
    assert data["fields_min"] == 1
    assert data["fields_max"] == 1
    assert data["truncation_count"] == 0


def test_dump_writes_text_only_when_asked(tmp_path, capsys):
    path = _make_workbook(tmp_path / "wb.xlsx")
    dump = tmp_path / "dump.json"
    with _fake_server(
        ok_body=_openai_body(OK_FIELDS, "stop"), trunc_body=b"", key=""
    ) as (_server, base, _state):
        code = _run(
            [
                str(path),
                "--provider", "openai",
                "--model", "test-model",
                "--base-url", base + "/ok",
                "--env-var", "",
                "--dump", str(dump),
            ]
        )
        out, err = capsys.readouterr()

    assert code == 0
    assert FIELD_LABEL not in out
    assert "may contain private data" in err
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["calls"][0]["prompt"]
