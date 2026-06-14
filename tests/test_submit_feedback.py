"""Unit tests for scripts/submit_feedback.py — /codeboarding-feedback capture."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import submit_feedback as sf  # noqa: E402

COMMAND = "/codeboarding-feedback"
HOST = "https://us.i.posthog.com"


def base_env(**overrides):
    env = {
        "COMMENT_BODY": f"{COMMAND} the diagram is great",
        "FEEDBACK_COMMAND": COMMAND,
        "REPOSITORY": "octo/repo",
        "REPOSITORY_ID": "555",
        "ISSUE_NUMBER": "42",
        "COMMENT_ID": "99",
        "COMMENT_URL": "https://github.com/octo/repo/pull/42#issuecomment-99",
        "AUTHOR_ASSOC": "CONTRIBUTOR",
        "SENDER_LOGIN": "octocat",
        "SENDER_ID": "1234",
        "GITHUB_RUN_ID": "777",
        "RUN_ATTEMPT": "1",
        "ACTION_REF": "v1",
    }
    env.update(overrides)
    return env


class TestExtractFeedback(unittest.TestCase):
    def test_extracts_text_after_command(self):
        self.assertEqual(sf.extract_feedback(f"{COMMAND} hello there", COMMAND), "hello there")

    def test_preserves_multiline_feedback(self):
        body = f"{COMMAND} first line\nsecond line\n\nfourth"
        self.assertEqual(sf.extract_feedback(body, COMMAND), "first line\nsecond line\n\nfourth")

    def test_command_only_yields_empty(self):
        self.assertEqual(sf.extract_feedback(COMMAND, COMMAND), "")
        self.assertEqual(sf.extract_feedback(f"{COMMAND}   ", COMMAND), "")

    def test_command_on_its_own_line_then_body(self):
        self.assertEqual(sf.extract_feedback(f"{COMMAND}\nthe body", COMMAND), "the body")

    def test_leading_whitespace_and_crlf_normalized(self):
        self.assertEqual(sf.extract_feedback(f"  {COMMAND} a\r\nb\r\n", COMMAND), "a\nb")

    def test_wrong_command_yields_empty(self):
        self.assertEqual(sf.extract_feedback("/codeboarding run it", COMMAND), "")
        self.assertEqual(sf.extract_feedback(f"{COMMAND}-typo hi", COMMAND), "")


class TestCapFeedback(unittest.TestCase):
    def test_short_text_not_truncated(self):
        self.assertEqual(sf.cap_feedback("abc", 10), ("abc", 3, False))

    def test_long_text_capped_and_marked(self):
        capped, length, truncated = sf.cap_feedback("x" * 50, 10)
        self.assertEqual(capped, "x" * 10)
        self.assertEqual(length, 50)
        self.assertTrue(truncated)


class TestOptOut(unittest.TestCase):
    def test_do_not_track_disables(self):
        self.assertTrue(sf.telemetry_disabled({"DO_NOT_TRACK": "1"}))
        self.assertTrue(sf.telemetry_disabled({"DO_NOT_TRACK": "true"}))

    def test_codeboarding_telemetry_false_disables(self):
        self.assertTrue(sf.telemetry_disabled({"CODEBOARDING_TELEMETRY": "false"}))

    def test_default_enabled(self):
        self.assertFalse(sf.telemetry_disabled({}))


class TestResolvers(unittest.TestCase):
    def test_key_and_host_defaults(self):
        self.assertEqual(sf.resolve_key({}), sf.DEFAULT_POSTHOG_KEY)
        self.assertEqual(sf.resolve_host({}), sf.DEFAULT_POSTHOG_HOST)

    def test_host_override_strips_trailing_slash(self):
        self.assertEqual(
            sf.resolve_host({"CODEBOARDING_POSTHOG_HOST": "https://eu.example.com/"}), "https://eu.example.com"
        )

    def test_max_chars_invalid_falls_back(self):
        self.assertEqual(sf.resolve_max_chars({"FEEDBACK_MAX_CHARS": "nope"}), sf.DEFAULT_MAX_CHARS)
        self.assertEqual(sf.resolve_max_chars({"FEEDBACK_MAX_CHARS": "0"}), sf.DEFAULT_MAX_CHARS)
        self.assertEqual(sf.resolve_max_chars({"FEEDBACK_MAX_CHARS": "25"}), 25)

    def test_distinct_id_prefers_sender_then_run(self):
        self.assertEqual(sf.distinct_id({"SENDER_ID": "5"}), "github-user:5")
        self.assertEqual(sf.distinct_id({"GITHUB_RUN_ID": "9"}), "github-run:9")


class TestBuildPayload(unittest.TestCase):
    def test_empty_feedback_returns_none(self):
        self.assertIsNone(sf.build_payload(base_env(COMMENT_BODY=COMMAND)))

    def test_payload_shape(self):
        payload = sf.build_payload(base_env())
        self.assertEqual(payload["event"], "codeboarding_feedback_submitted")
        self.assertEqual(payload["distinct_id"], "github-user:1234")
        self.assertEqual(payload["api_key"], sf.DEFAULT_POSTHOG_KEY)
        props = payload["properties"]
        self.assertEqual(props["source"], "github_action_feedback")
        self.assertEqual(props["command"], COMMAND)
        self.assertEqual(props["feedback_text"], "the diagram is great")
        self.assertEqual(props["feedback_length"], len("the diagram is great"))
        self.assertFalse(props["feedback_truncated"])
        self.assertEqual(props["repository"], "octo/repo")
        self.assertEqual(props["repository_id"], "555")
        self.assertEqual(props["pr_number"], "42")
        self.assertEqual(props["comment_id"], "99")
        self.assertEqual(props["author_association"], "CONTRIBUTOR")
        self.assertEqual(props["sender_login"], "octocat")
        self.assertEqual(props["run_id"], "777")

    def test_truncation_recorded_in_payload(self):
        payload = sf.build_payload(base_env(COMMENT_BODY=f"{COMMAND} " + "y" * 50, FEEDBACK_MAX_CHARS="10"))
        props = payload["properties"]
        self.assertEqual(len(props["feedback_text"]), 10)
        self.assertEqual(props["feedback_length"], 50)
        self.assertTrue(props["feedback_truncated"])

    def test_optional_props_omitted_when_absent(self):
        payload = sf.build_payload({"COMMENT_BODY": f"{COMMAND} hi", "SENDER_ID": "1"})
        self.assertNotIn("repository", payload["properties"])
        self.assertNotIn("comment_url", payload["properties"])


class TestMain(unittest.TestCase):
    def _run(self, env):
        with mock.patch.object(sf.urllib.request, "urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = sf.main(env)
        return rc, urlopen, buf.getvalue()

    def test_sends_expected_json_shape(self):
        rc, urlopen, _ = self._run(base_env())
        self.assertEqual(rc, 0)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"{HOST}/i/v0/e/")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers.get("Content-type"), "application/json")
        body = json.loads(request.data)
        self.assertEqual(body["event"], "codeboarding_feedback_submitted")
        self.assertEqual(body["distinct_id"], "github-user:1234")
        self.assertEqual(body["properties"]["feedback_text"], "the diagram is great")

    def test_host_override_used(self):
        _, urlopen, _ = self._run(base_env(CODEBOARDING_POSTHOG_HOST="https://eu.example.com"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://eu.example.com/i/v0/e/")

    def test_do_not_track_skips_sending(self):
        _, urlopen, out = self._run(base_env(DO_NOT_TRACK="1"))
        urlopen.assert_not_called()
        self.assertIn("disabled", out)

    def test_telemetry_false_skips_sending(self):
        _, urlopen, _ = self._run(base_env(CODEBOARDING_TELEMETRY="false"))
        urlopen.assert_not_called()

    def test_empty_feedback_not_sent(self):
        _, urlopen, out = self._run(base_env(COMMENT_BODY=COMMAND))
        urlopen.assert_not_called()
        self.assertIn("nothing to send", out)

    def test_does_not_print_feedback_text(self):
        secret = "PLEASE_DO_NOT_LEAK_THIS_abc123"
        _, _, out = self._run(base_env(COMMENT_BODY=f"{COMMAND} {secret}"))
        self.assertNotIn(secret, out)

    def test_network_failure_is_swallowed(self):
        with mock.patch.object(sf.urllib.request, "urlopen", side_effect=sf.urllib.error.URLError("down")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = sf.main(base_env())
        self.assertEqual(rc, 0)
        self.assertIn("ignoring", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
