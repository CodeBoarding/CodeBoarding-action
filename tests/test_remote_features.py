"""Direct endpoint, source and provider contracts for the optional review requests."""

import importlib.util
import json
import lzma
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from importlib import metadata
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "remote_features.py"
_SPEC = importlib.util.spec_from_file_location("remote_features", _SCRIPT)
remote_features = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = remote_features
_SPEC.loader.exec_module(remote_features)

INPUTS = {"head.json", "base.json", "metadata.json", "pr.json", "pr-files.json", "files.json"}
GUIDE = {"version": 1, "changes": [{"id": "u1"}]}
ENVELOPE = {
    "feature": "architecture",
    "schema": 1,
    "status": "partial",
    "repo": "o/r",
    "pr": 7,
    "headSha": "H",
    "generator": {"model": "m", "hosted": True, "version": "v"},
    "coverage": {"languages": ["Python"], "files": {"seen": 2, "total": 2}},
    "data": {"findings": []},
    "reason": "source omitted",
}


class _Server:
    def __init__(self, respond):
        outer = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(
                    {"path": self.path, "method": self.command, "headers": dict(self.headers), "body": body}
                )
                status, payload = respond(self.path, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


def _workspace(tmp):
    artifact, head, base, checkout = (tmp / n for n in ("artifact", "head", "base", "checkout"))
    for directory in (artifact, head, base, checkout):
        directory.mkdir()
    (artifact / "metadata.json").write_text(json.dumps({"head_sha": "H", "base_sha": "TIP", "merge_base_sha": "B"}))
    for directory in (head, base):
        (directory / "analysis.json").write_text('{"components": []}')
        (directory / "static_analysis.pkl").touch()
    (checkout / "app.py").write_text("head content")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "app.py"], check=True)
    return {
        "CLOSED_FUNCTIONS_URL": "http://127.0.0.1:9",
        "REPOSITORY": "o/r",
        "PR_NUMBER": "7",
        "ARTIFACT_DIR": str(artifact),
        "HEAD_ANALYSIS_PATH": str(head / "analysis.json"),
        "BASE_ANALYSIS_PATH": str(base / "analysis.json"),
        "CHECKOUT_DIR": str(checkout),
        "GITHUB_TOKEN": "ghs_run",
        "RUNNER_TEMP": str(tmp),
    }


def _github_json(path):
    if "/compare/" in path:
        return {"files": [{"filename": "app.py", "status": "modified"}, {"filename": "new.py", "status": "added"}]}
    if "/commits?" in path:
        return [{"commit": {"message": "Return two\n\nbody"}}]
    return {"title": "Two", "body": "Returns two"}


def _read(env, name):
    return json.loads((Path(env["ARTIFACT_DIR"]) / name).read_text())


class BundleTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(remote_features, "read_analysis", return_value=(_analysis([]), {}))
        patch.start()
        self.addCleanup(patch.stop)

    def test_analysed_commits_and_both_sides(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _workspace(Path(tmp))
            with (
                mock.patch.object(remote_features, "_github_json", side_effect=_github_json) as gh,
                mock.patch.object(remote_features, "_contents", return_value="base content") as base,
                mock.patch.object(remote_features, "call_graph", return_value={"nodes": {}, "edges": []}),
            ):
                bundle = remote_features.collect(env)
            self.assertIn("/repos/o/r/compare/B...H", [c.args[0] for c in gh.call_args_list])
            self.assertEqual(json.loads(bundle["files.json"]), {"H:app.py": "head content", "B:app.py": "base content"})
            base.assert_called_once_with("o/r", "B", "app.py")
            self.assertEqual(json.loads(bundle["metadata.json"])["base_sha"], "B")
            self.assertEqual(json.loads(bundle["pr.json"])["commits"], ["Return two"])
            self.assertEqual(set(bundle), INPUTS)
            self.assertEqual(json.loads(bundle["head.json"])["version"], 1)

    def test_a_changed_file_over_the_size_limit_is_left_out_only_at_that_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _workspace(Path(tmp))
            with (
                mock.patch.object(remote_features, "_github_json", side_effect=_github_json),
                mock.patch.object(remote_features, "_contents", return_value="b" * 13),
                mock.patch.object(remote_features, "call_graph", return_value={"nodes": {}, "edges": []}),
                mock.patch.object(remote_features, "MAX_FILE_BYTES", 12),
            ):
                self.assertEqual(json.loads(remote_features.collect(env)["files.json"]), {"H:app.py": "head content"})

    def test_changed_file_symlink_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = _workspace(Path(tmp))
            path = Path(env["CHECKOUT_DIR"]) / "app.py"
            path.unlink()
            path.symlink_to(Path(env["ARTIFACT_DIR"]) / "metadata.json")
            with (
                mock.patch.object(remote_features, "_github_json", side_effect=_github_json),
                mock.patch.object(remote_features, "_contents", return_value=None),
                mock.patch.object(remote_features, "call_graph", return_value={}),
            ):
                self.assertEqual(json.loads(remote_features.collect(env)["files.json"]), {})


def _method(name, start, end, content_hash):
    return SimpleNamespace(
        qualified_name=name, start_line=start, end_line=end, node_type="FUNCTION", content_hash=content_hash
    )


def _component(cid, name=None, members=None, description=None):
    """A component as Core parses it; members are qualified names by file."""
    groups = [
        SimpleNamespace(file_path=path, methods=[SimpleNamespace(qualified_name=q) for q in names])
        for path, names in (members or {}).items()
    ]
    return SimpleNamespace(component_id=cid, name=name, description=description, file_methods=groups)


def _relation(src, dst, label, *edges):
    """A relation as Core parses it; edges are `path|qualified_name` pairs."""

    def reference(key):
        path, name = key.split("|")
        return SimpleNamespace(reference_file=path, qualified_name=name)

    return SimpleNamespace(
        src_id=src,
        dst_id=dst,
        relation=label,
        all_edges=[SimpleNamespace(source=reference(s), target=reference(t)) for s, t in edges],
    )


def _analysis(components, relations=(), files=None):
    """A scope as Core parses it: the root carries the file index, a sub-analysis does not need one."""
    return SimpleNamespace(
        components=list(components),
        components_relations=list(relations),
        files={path: SimpleNamespace(methods=methods) for path, methods in (files or {}).items()},
    )


class SideDocumentTests(unittest.TestCase):
    """The service's bundle.expand_side reads this exact document back (its tests hold the same one)."""

    ROOT = _analysis(
        [
            _component(
                "1",
                "Core",
                {"pkg/a.py": ["pkg.a.run", "pkg.a.helper"], "pkg/b.py": ["pkg.b.save", "~Weird"]},
                description="d1",
            ),
            _component("2", "Also", {"pkg/b.py": ["pkg.b.save"], "pkg/c.py": ["pkg.c.gone"]}),
        ],
        [_relation("1", "2", "uses", ("pkg/a.py|pkg.a.run", "pkg/b.py|pkg.b.save"))],
        files={
            "pkg/a.py": [_method("pkg.a.run", 1, 5, "h1"), _method("pkg.a.helper", 6, 8, "h2")],
            "pkg/b.py": [_method("pkg.b.save", 1, 4, "h3"), _method("~Weird", 10, 12, "h4")],
        },
    )
    SUB_ANALYSES = {
        "1": _analysis(
            [
                _component("1.1", "Run", {"pkg/a.py": ["pkg.a.run", "pkg.a.helper"]}),
                _component("1.2", "Store", {"pkg/b.py": ["pkg.b.save", "~Weird"]}),
            ],
            [
                _relation(
                    "1.1",
                    "1.2",
                    "saves",
                    ("pkg/a.py|pkg.a.run", "pkg/b.py|pkg.b.save"),
                    ("pkg/a.py|pkg.a.helper", "pkg/b.py|pkg.b.save"),
                )
            ],
        )
    }
    GRAPH = {
        "nodes": {
            "pkg.a.run": {"file": "pkg/a.py", "start": 1, "end": 5, "cb": False},
            "pkg.a.helper": {"file": "pkg/a.py", "start": 6, "end": 9, "cb": False},
            "pkg.b.save": {"file": "pkg/b.py", "start": 1, "end": 4, "cb": False},
            "pkg.x.Only": {"file": "pkg/x.py", "start": 1, "end": 2, "cb": True},
        },
        "edges": [
            {"s": "pkg.a.run", "d": "pkg.b.save", "lines": [3, 4]},
            {"s": "pkg.a.run", "d": "pkg.a.helper", "lines": [2]},
            {"s": "pkg.b.save", "d": "pkg.a.run", "lines": [2]},
            {"s": "pkg.x.Only", "d": "ext.lib.call", "lines": []},
        ],
    }
    DOCUMENT = {
        "version": 1,
        "files": [
            ["pkg/a.py", [["~.run", 1, 5, "FUNCTION", "h1", 0], ["~.helper", 6, 8, "FUNCTION", "h2", 0]]],
            ["pkg/b.py", [["~.save", 1, 4, "FUNCTION", "h3", 0], ["~~Weird", 10, 12, "FUNCTION", "h4", -1]]],
            ["pkg/x.py", [["~.Only", 1, 2, 1]]],
        ],
        "unplaced": ["ext.lib.call"],
        "graph_spans": [[1, 6, 9]],
        "components": [
            ["1", "Core", "d1", -1, None, [["1.1", "1.2", "saves"]]],
            ["1.1", "Run", None, 0, [0, 1], []],
            ["1.2", "Store", None, 0, [2, 3], []],
            ["2", "Also", None, -1, [2, "pkg/c.py|pkg.c.gone"], []],
        ],
        "relations": [["1", "2", "uses"]],
        "calls": [[0, [[2, 3, 4], [1, 2]]], [2, [[0, 2]]], [4, [[5]]]],
        "extra_edges": [[1, 2]],
    }

    def test_each_fact_once_and_only_what_the_service_reads(self):
        doc = json.loads(json.dumps(remote_features.side_document(self.ROOT, self.SUB_ANALYSES, self.GRAPH)))
        self.assertEqual(doc, self.DOCUMENT)

    def test_a_parent_is_left_out_only_when_its_childrens_members_rebuild_it(self):
        # The service rebuilds a parent from its children's members, so a parent holding fewer than a
        # child does is written out even though its leaves hold exactly its members.
        def component(cid, names):
            return _component(cid, members={"a.py": list(names)})

        root = _analysis(
            [component("G", "xy"), component("P", "xy")],
            files={"a.py": [_method(q, i, i, "h") for i, q in enumerate("xyz", 1)]},
        )
        sub_analyses = {
            "G": _analysis([component("M", "xyz")]),
            "M": _analysis([component("L1", "x"), component("L2", "y")]),
            "P": _analysis([component("C1", "x"), component("C2", "y")]),
        }
        members = [c[4] for c in remote_features.side_document(root, sub_analyses, {})["components"]]
        self.assertEqual(members, [[0, 1], [0, 1, 2], [0], [1], None, [0], [1]])


def _core_installed() -> bool:
    try:
        metadata.version("codeboarding")
    except metadata.PackageNotFoundError:
        return False
    return True


@unittest.skipUnless(_core_installed(), "the pinned CodeBoarding release is not installed")
class InstalledCoreTests(unittest.TestCase):
    """read_analysis and side_document against the release action.yml pins. It runs in the
    `core-compatibility` CI job.

    tests/fixtures/analysis holds one analysis as two Core releases wrote it: core-0.15.5.json and
    core-9cf50fb9.json (main after #571). Each release reads only its own, so the pinned one must read at
    least one, and every one it reads must give the same document. A pin whose release reads none needs
    that analysis written by it, which is the point: the document has not been checked against its parser."""

    GRAPH = {
        "nodes": {
            **SideDocumentTests.GRAPH["nodes"],
            "pkg.c.load": {"file": "pkg/c.py", "start": 1, "end": 3, "cb": False},
        },
        "edges": [*SideDocumentTests.GRAPH["edges"], {"s": "pkg.c.load", "d": "pkg.b.save", "lines": [2]}],
    }
    DOCUMENT = {
        "version": 1,
        "files": [
            ["pkg/a.py", [["~.run", 1, 5, "FUNCTION", "h1", 0], ["~.helper", 6, 8, "FUNCTION", "h2", 0]]],
            ["pkg/b.py", [["~.save", 1, 4, "FUNCTION", "h3", 0], ["~~Weird", 10, 12, "FUNCTION", "h4", -1]]],
            ["pkg/c.py", [["~.load", 1, 3, "FUNCTION", "h5", 0]]],
            ["pkg/x.py", [["~.Only", 1, 2, 1]]],
        ],
        "unplaced": ["ext.lib.call"],
        "graph_spans": [[1, 6, 9]],
        "components": [
            ["1", "Core", "d1", -1, None, [["1.1", "1.2", "saves"]]],
            ["1.1", "Run", "", 0, [0, 1], []],
            ["1.2", "Store", "", 0, [2, 3], []],
            ["2", "Also", "", -1, [4], []],
        ],
        "relations": [["1.1", "1.2", "saves"], ["1.1", "2", "uses"], ["1.2", "2", "loads"], ["2", "1.2", "reads"]],
        "calls": [[0, [[2, 3, 4], [1, 2]]], [2, [[0, 2]]], [4, [[2, 2]]], [5, [[6]]]],
        # Of the analysis's edges, the calls are left to the graph: load -> save is one.
        "extra_edges": [[0, 4], [1, 2], [3, 4]],
    }

    def test_the_pinned_release_reads_an_analysis_it_wrote_into_the_side_document(self):
        read = []
        for fixture in sorted((Path(__file__).parent / "fixtures" / "analysis").glob("*.json")):
            try:
                root, sub_analyses = remote_features.read_analysis(fixture)
            except (TypeError, ValueError):  # another release's format
                continue
            read.append(fixture.name)
            with self.subTest(fixture=fixture.name):
                doc = json.loads(json.dumps(remote_features.side_document(root, sub_analyses, self.GRAPH)))
                self.assertEqual(doc, self.DOCUMENT)
        self.assertTrue(
            read, "the pinned release reads none of tests/fixtures/analysis; add the analysis as it writes it"
        )


class CredentialTests(unittest.TestCase):
    def test_without_oidc_the_runs_own_token_proves_the_repository(self):
        self.assertEqual(remote_features._bearer({"GITHUB_TOKEN": "ghs_run"}), {"Authorization": "Bearer ghs_run"})

    def test_oidc_bearer_includes_license_and_removes_temporary_file(self):
        mint = _Server(lambda p, b: (200, {"value": "jwt.token.sig"}))
        self.addCleanup(mint.close)
        with tempfile.TemporaryDirectory() as tmp:
            headers = remote_features._bearer(
                {
                    "ACTIONS_ID_TOKEN_REQUEST_URL": f"{mint.url}/token?x=1",
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req",
                    "LICENSE_KEY": "LIC",
                    "RUNNER_TEMP": tmp,
                }
            )
            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertEqual(headers, {"Authorization": "Bearer jwt.token.sig~codeboarding-license~LIC"})
        self.assertIn("audience=codeboarding-proxy", mint.requests[0]["path"])

    def test_hosted_and_license_send_no_model(self):
        for llm in ("", "hosted", "license"):
            self.assertEqual(remote_features.model_headers({"LLM": llm, "OPENAI_API_KEY": "leftover"}), {})

    def test_each_fixed_provider_and_analysis_model(self):
        for provider, host in remote_features.PROVIDER_HOSTS.items():
            with self.subTest(provider=provider):
                env = {
                    "LLM": provider,
                    f"{provider.upper()}_API_KEY": " k ",
                    "MODEL": "general",
                    "AGENT_MODEL": "analysis",
                }
                if provider == "aws_bedrock":
                    env["AWS_BEDROCK_REGION"] = "eu-west-1"
                    host = host.format(region="eu-west-1")
                env[f"{provider.upper()}_BASE_URL"] = f"https://{host}/v1"
                headers = remote_features.model_headers(env)
                self.assertEqual(headers["x-llm-api-key"], "k")
                self.assertEqual(headers["x-llm-model"], "analysis")
                if provider == "aws_bedrock":
                    self.assertEqual(headers["x-llm-region"], "eu-west-1")

    def test_key_is_unwrapped_the_way_the_analysis_reads_it(self):
        bedrock = {"LLM": "aws_bedrock", "AWS_BEDROCK_REGION": "us-east-1"}
        for env, want in (
            ({"LLM": "anthropic", "ANTHROPIC_API_KEY": ' ANTHROPIC_API_KEY="sk-ant-test"\n'}, "sk-ant-test"),
            ({"LLM": "openai", "OPENAI_API_KEY": "'sk-test'"}, "sk-test"),
            ({**bedrock, "AWS_BEDROCK_API_KEY": "AWS_BEARER_TOKEN_BEDROCK=abc="}, "abc="),
        ):
            with self.subTest(llm=env["LLM"]):
                self.assertEqual(remote_features.model_headers(env)["x-llm-api-key"], want)

    def test_unsupported_self_hosted_and_missing_key_never_fall_back(self):
        for env in (
            {"LLM": "litellm", "LITELLM_API_KEY": "k"},
            {"LLM": "ollama", "OLLAMA_API_KEY": "k", "OLLAMA_BASE_URL": "http://10.0.0.5:11434"},
            {"LLM": "openai", "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "https://gateway.corp/v1"},
            {"LLM": "anthropic"},
        ):
            self.assertEqual(remote_features.model_headers(env), {"x-llm-provider": env["LLM"]})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = _workspace(Path(tmp.name))
        for name, kwargs in (
            ("_github_json", {"side_effect": _github_json}),
            ("_contents", {"return_value": "base"}),
            ("call_graph", {"return_value": {"nodes": {}, "edges": []}}),
            ("read_analysis", {"return_value": (_analysis([]), {})}),
        ):
            patch = mock.patch.object(remote_features, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    def run_service(self, respond=None):
        service = _Server(
            respond
            or (
                lambda p, b: (
                    200,
                    (
                        {"guide": GUIDE, "usage": {}, "timingsMs": {}}
                        if p == "/v1/review-guide"
                        else {"envelope": ENVELOPE, "usage": {}}
                    ),
                )
            )
        )
        self.addCleanup(service.close)
        self.env["CLOSED_FUNCTIONS_URL"] = service.url + "/"
        with mock.patch("builtins.print"):
            self.assertEqual(remote_features.main(self.env), 0)
        return service

    def test_exactly_two_posts_shared_inputs_and_fixed_outputs(self):
        service = self.run_service()
        self.assertEqual(_read(self.env, "review_guide.json"), GUIDE)
        self.assertEqual(_read(self.env, "closed/architecture.json"), ENVELOPE)
        self.assertEqual(
            sorted(p.name for p in (Path(self.env["ARTIFACT_DIR"]) / "closed").iterdir()), ["architecture.json"]
        )
        self.assertEqual(sorted(r["path"] for r in service.requests), ["/v1/architecture", "/v1/review-guide"])
        bodies = {}
        for request in service.requests:
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["headers"]["Authorization"], "Bearer ghs_run")
            bodies[request["path"]] = json.loads(lzma.decompress(request["body"]))
        guide, arch = bodies["/v1/review-guide"], bodies["/v1/architecture"]
        self.assertEqual(set(guide["files"]), INPUTS)
        self.assertEqual(set(arch["files"]), INPUTS | {"source.json"})
        self.assertEqual({k: arch["files"][k] for k in INPUTS}, guide["files"])
        self.assertEqual((arch["version"], arch["repo"], arch["pr"]), (1, "o/r", 7))

    def test_http_failures_isolated_in_both_directions(self):
        self.run_service(
            lambda p, b: (500, {"message": "broken"}) if p.endswith("architecture") else (200, {"guide": GUIDE})
        )
        self.assertEqual(_read(self.env, "review_guide.json"), GUIDE)
        self.assertEqual(_read(self.env, "closed/architecture.json")["status"], "failed")
        (Path(self.env["ARTIFACT_DIR"]) / "review_guide.json").unlink()
        self.run_service(lambda p, b: (500, {}) if p.endswith("review-guide") else (200, {"envelope": ENVELOPE}))
        self.assertEqual(_read(self.env, "closed/architecture.json"), ENVELOPE)
        self.assertFalse((Path(self.env["ARTIFACT_DIR"]) / "review_guide.json").exists())

    def test_malformed_responses_do_not_block_other_call(self):
        self.run_service(lambda p, b: (200, []) if p.endswith("architecture") else (200, {"guide": GUIDE}))
        self.assertEqual(_read(self.env, "review_guide.json"), GUIDE)
        self.assertEqual(_read(self.env, "closed/architecture.json")["status"], "failed")

    def test_quota_is_skipped(self):
        self.run_service(lambda p, b: (402, {"message": "token limit"}))
        self.assertEqual(_read(self.env, "closed/architecture.json")["status"], "skipped")

    def test_all_architecture_statuses_preserved(self):
        for status in ("ok", "partial", "skipped", "failed"):
            envelope = {**ENVELOPE, "status": status}
            self.run_service(lambda p, b: (200, {"guide": GUIDE, "envelope": envelope}))
            self.assertEqual(_read(self.env, "closed/architecture.json"), envelope)

    def test_unsupported_provider_still_gets_measured_architecture(self):
        self.env.update(LLM="litellm", LITELLM_API_KEY="secret")
        service = self.run_service()
        self.assertEqual([r["path"] for r in service.requests], ["/v1/architecture"])
        headers = {k.lower(): v for k, v in service.requests[0]["headers"].items()}
        self.assertEqual(headers["x-llm-provider"], "litellm")
        self.assertNotIn("x-llm-api-key", headers)
        self.assertFalse((Path(self.env["ARTIFACT_DIR"]) / "review_guide.json").exists())

    def test_missing_shared_input_records_failure_without_requests(self):
        (Path(self.env["HEAD_ANALYSIS_PATH"]).parent / "static_analysis.pkl").unlink()
        self.assertEqual(self.run_service().requests, [])
        self.assertEqual(_read(self.env, "closed/architecture.json")["status"], "failed")

    def test_both_requests_can_run_concurrently(self):
        barrier = threading.Barrier(2)

        def respond(path, body):
            barrier.wait(timeout=5)
            return 200, {"guide": GUIDE, "envelope": ENVELOPE}

        self.run_service(respond)
        self.assertEqual(_read(self.env, "review_guide.json"), GUIDE)
        self.assertEqual(_read(self.env, "closed/architecture.json"), ENVELOPE)

    def test_empty_url_disables_both(self):
        self.env["CLOSED_FUNCTIONS_URL"] = ""
        with mock.patch.object(remote_features, "_post") as post:
            self.assertEqual(remote_features.main(self.env), 0)
            post.assert_not_called()
        self.assertFalse((Path(self.env["ARTIFACT_DIR"]) / "closed").exists())

    def test_source_toggle_and_read_failure_only_omit_architecture_source(self):
        for failure in (False, True):
            self.env["CLOSED_FEATURES_SOURCE"] = "false"
            with (
                mock.patch.object(remote_features, "collect_source", side_effect=OSError())
                if failure
                else nullcontext()
            ):
                service = self.run_service()
            for request in service.requests:
                body = json.loads(lzma.decompress(request["body"]))
                self.assertNotIn("source.json", body["files"])
                self.assertEqual("omitted" in body, request["path"] == "/v1/architecture")

    def test_either_limit_omits_source_then_skips_if_required_inputs_too_large(self):
        with mock.patch.object(remote_features, "collect_source", return_value=(None, "off")):
            without_source = max(len(lzma.decompress(r["body"])) for r in self.run_service().requests)
        # Random source is over the compressed limit; repeated source compresses to almost nothing and is
        # over the JSON limit only.
        for limit, room, source in (
            ("MAX_COMPRESSED_BYTES", 1000, os.urandom(2000).hex()),
            ("MAX_FEATURE_JSON_BYTES", without_source + 1000, "x" * 100_000),
        ):
            with (
                mock.patch.object(remote_features, limit, room),
                mock.patch.object(remote_features, "collect_source", return_value=(source, None)),
            ):
                service = self.run_service()
            arch = next(r for r in service.requests if r["path"] == "/v1/architecture")
            body = json.loads(lzma.decompress(arch["body"]))
            self.assertNotIn("source.json", body["files"])
            self.assertIn("too large", body["omitted"]["source.json"])
            with mock.patch.object(remote_features, limit, 1):
                self.assertEqual(self.run_service().requests, [])
            self.assertEqual(_read(self.env, "closed/architecture.json")["status"], "skipped")


class SourceTests(unittest.TestCase):
    def test_tracked_extensions_exclusions_limits_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", tmp], check=True)
            names = [
                "a.py",
                "a.ts",
                "a.tsx",
                "a.js",
                "a.jsx",
                "a.mjs",
                "a.cjs",
                "a.cs",
                "a.csproj",
                "b.txt",
                "docs/d.py",
                "node_modules/n.js",
                "a.min.js",
                "a.d.ts",
                "binary.py",
                "big.py",
            ]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x")
            (root / "binary.py").write_bytes(b"\xff")
            (root / "big.py").write_text("x" * 300_001)
            (root / "link.py").symlink_to(root / "a.py")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            (root / "untracked.py").write_text("not tracked")
            text, why = remote_features.collect_source({"CHECKOUT_DIR": tmp})
            self.assertEqual(set(json.loads(text)), set(names[:9]))
            self.assertIsNone(why)
            with mock.patch.object(remote_features, "MAX_SOURCE_BYTES", 1):
                text, why = remote_features.collect_source({"CHECKOUT_DIR": tmp})
                self.assertIsNone(text)
                self.assertIn("limit", why)

    def test_symlink_parent_and_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "link").symlink_to(root, target_is_directory=True)
            for name in ("link/a.py", "../a.py", "/tmp/a.py"):
                with self.assertRaises(OSError):
                    remote_features._checkout_file(root, name)

    def test_disabled_source_does_not_read_checkout(self):
        self.assertEqual(
            remote_features.collect_source({"CLOSED_FEATURES_SOURCE": "false"}), (None, "turned off by the workflow")
        )


if __name__ == "__main__":
    unittest.main()
