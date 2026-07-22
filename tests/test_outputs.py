"""Characterization tests for output formatting (_strip_ansi, _format_outputs)."""
import config
import outputs


class TestStripAnsi:
    def test_removes_color_codes(self):
        assert outputs._strip_ansi("\x1b[31mred\x1b[0m plain") == "red plain"

    def test_plain_text_untouched(self):
        assert outputs._strip_ansi("no ansi here") == "no ansi here"


class TestFormatOutputs:
    def test_empty(self):
        assert outputs._format_outputs([]) == ([], "")

    def test_stream(self):
        structured, text = outputs._format_outputs(
            [{"output_type": "stream", "name": "stdout", "text": "hello\n"}])
        assert structured == [{"type": "stream", "name": "stdout", "text": "hello\n"}]
        assert text == "hello\n"

    def test_execute_result_plain(self):
        structured, text = outputs._format_outputs(
            [{"output_type": "execute_result", "data": {"text/plain": "42"}}])
        assert structured == [{"type": "execute_result", "text": "42"}]
        assert text == "42"

    def test_image_becomes_size_marker_not_bytes(self):
        # INCLUDE_IMAGE_BYTES=False: mobile payloads stay small; images -> markers.
        b64 = "A" * 5000
        structured, text = outputs._format_outputs(
            [{"output_type": "display_data", "data": {"text/plain": "<Figure>", "image/png": b64}}])
        (item,) = structured
        assert item["images"] == [{"mime": "image/png", "size_b64": 5000}]
        assert "bytes_b64" not in item["images"][0]
        assert "[image/png 5000 b64 chars]" in text

    def test_error_traceback_ansi_stripped(self):
        structured, text = outputs._format_outputs([{
            "output_type": "error", "ename": "ValueError", "evalue": "boom",
            "traceback": ["\x1b[31mTraceback\x1b[0m", "ValueError: boom"],
        }])
        (item,) = structured
        assert item == {"type": "error", "ename": "ValueError", "evalue": "boom",
                        "traceback": "Traceback\nValueError: boom"}
        assert text.startswith("ValueError: boom")
        assert "\x1b" not in text

    def test_truncation_at_max_output_chars(self):
        big = "x" * (config.MAX_OUTPUT_CHARS + 5000)
        _, text = outputs._format_outputs([{"output_type": "stream", "name": "stdout", "text": big}])
        assert text.startswith("x" * config.MAX_OUTPUT_CHARS)
        assert text.endswith(f"[truncated, {len(big)} chars total]")
        assert len(text) < len(big)
