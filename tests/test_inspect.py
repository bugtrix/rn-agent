"""Inspect tools for the interactive chat: files, npm, and the public web."""

from __future__ import annotations

from rn_agent.agents.inspect import parse_tool, run_tool
from rn_agent.utils.io import atomic_write_text


def test_parse_tool_reads_a_json_object():
    payload = parse_tool('{"tool":"read","path":"ios/Podfile"}')

    assert payload == {"tool": "read", "path": "ios/Podfile"}


def test_parse_tool_ignores_ordinary_prose():
    assert parse_tool("The Podfile is a standard RN template.") is None


def test_parse_tool_ignores_prose_that_mentions_json():
    text = (
        "Two options remain. Dynamic frameworks, or set RNFirebaseDisableSPM. "
        'A tool call would look like {"tool":"read","path":"ios/Podfile"} but I '
        "already have enough to answer you without another lookup."
    )

    assert parse_tool(text) is None


def test_read_returns_the_file(project):
    context = project.scanned()
    call = run_tool(context, {"tool": "read", "path": "package.json"})

    assert call.name == "read"
    assert '"react-native"' in call.result
    assert "package.json" in call.detail


def test_read_refuses_a_secret_file(project):
    atomic_write_text(project.root / ".env", "SECRET=1\n")
    context = project.scanned()

    call = run_tool(context, {"tool": "read", "path": ".env"})

    assert "Refused" in call.result


def test_grep_finds_a_line(project):
    context = project.scanned()
    call = run_tool(context, {"tool": "grep", "pattern": "react-native", "path": "package.json"})

    assert "package.json" in call.result
    assert "matches" in call.summary


def test_glob_lists_source_files(project):
    context = project.scanned()
    call = run_tool(context, {"tool": "glob", "pattern": "src/**/*.tsx"})

    assert "src/components/Button.tsx" in call.result


def test_parse_tool_accepts_npm():
    payload = parse_tool('{"tool":"npm","package":"react-native"}')

    assert payload == {"tool": "npm", "package": "react-native"}


def test_npm_reports_the_registry_latest(project, monkeypatch):
    from rn_agent.net.http import HttpResponse
    from rn_agent.upgrade.registry import NpmRegistry

    class Transport:
        def request(self, method, url, *, headers, payload=None, timeout=30.0):
            return HttpResponse(
                status=200,
                body={
                    "name": "react-native",
                    "dist-tags": {"latest": "0.81.1"},
                    "versions": {
                        "0.80.0": {"version": "0.80.0"},
                        "0.81.1": {"version": "0.81.1"},
                    },
                },
            )

    monkeypatch.setattr(
        "rn_agent.agents.inspect.NpmRegistry",
        lambda **kwargs: NpmRegistry(transport=Transport()),
    )
    call = run_tool(project.scanned(), {"tool": "npm", "package": "react-native"})

    assert call.name == "npm"
    assert "0.81.1" in call.result
    assert "registry.npmjs.org" in call.result


def test_npm_refuses_a_url(project):
    call = run_tool(project.scanned(), {"tool": "npm", "package": "https://evil.example"})

    assert "valid npm package" in call.result


def test_parse_tool_accepts_search_and_fetch():
    assert parse_tool('{"tool":"search","query":"latest React Native"}') == {
        "tool": "search",
        "query": "latest React Native",
    }
    assert parse_tool('{"tool":"fetch","url":"https://reactnative.dev/blog"}') == {
        "tool": "fetch",
        "url": "https://reactnative.dev/blog",
    }


def test_search_returns_web_hits(project, monkeypatch):
    from rn_agent.net.http import HttpResponse

    markup = """
    <a class="result__a" href="https://reactnative.dev/blog">React Native 0.81</a>
    <a class="result__snippet">0.81.1 is current.</a>
    """

    class Transport:
        def request(self, method, url, *, headers, payload=None, timeout=30.0):
            return HttpResponse(status=200, body={}, text=markup)

    monkeypatch.setattr("rn_agent.agents.web.default_transport", lambda: Transport())
    call = run_tool(project.scanned(), {"tool": "search", "query": "latest React Native"})

    assert call.name == "search"
    assert "reactnative.dev/blog" in call.result
    assert "results" in call.summary


def test_fetch_refuses_localhost(project):
    call = run_tool(project.scanned(), {"tool": "fetch", "url": "https://127.0.0.1/"})

    assert call.name == "fetch"
    assert "Refused" in call.result
    assert call.summary == "refused"


def test_parse_tool_accepts_write_and_delete():
    payload = parse_tool('{"tool":"write","path":"src/App.tsx","content":"export default 1;\\n"}')

    assert payload is not None
    assert payload["tool"] == "write"
    assert "export default 1;" in payload["content"]
    assert parse_tool('{"tool":"delete","path":"src/Old.tsx"}') == {
        "tool": "delete",
        "path": "src/Old.tsx",
    }


def test_write_queues_a_new_file(project):
    call = run_tool(
        project.scanned(),
        {"tool": "write", "path": "src/Hello.tsx", "content": "export const Hello = 1;\n"},
    )

    assert call.name == "write"
    assert len(call.edits) == 1
    assert call.edits[0].path == "src/Hello.tsx"
    assert call.edits[0].action.value == "create"
    assert not (project.root / "src" / "Hello.tsx").exists()


def test_write_refuses_a_secret_file(project):
    call = run_tool(project.scanned(), {"tool": "write", "path": ".env", "content": "SECRET=1\n"})

    assert call.edits == ()
    assert "Refused" in call.result


def test_delete_queues_a_path(project):
    call = run_tool(project.scanned(), {"tool": "delete", "path": "src/components/Button.tsx"})

    assert len(call.edits) == 1
    assert call.edits[0].action.value == "delete"
    assert (project.root / "src" / "components" / "Button.tsx").exists()


def test_rename_queues_create_and_delete(project):
    source = "src/screens/HomeScreen.tsx"
    dest = "src/screens/DiscoverScreen.tsx"
    original = (project.root / source).read_text()

    call = run_tool(project.scanned(), {"tool": "rename", "from": source, "to": dest})

    assert call.name == "rename"
    assert [edit.action.value for edit in call.edits] == ["create", "delete"]
    assert call.edits[0].path == dest
    assert call.edits[0].content == original
    assert call.edits[1].path == source
    assert (project.root / source).exists()
    assert not (project.root / dest).exists()


def test_rename_uses_supplied_content(project):
    call = run_tool(
        project.scanned(),
        {
            "tool": "rename",
            "from": "src/screens/HomeScreen.tsx",
            "to": "src/screens/DiscoverScreen.tsx",
            "content": "export const DiscoverScreen = () => null;\n",
        },
    )

    assert "DiscoverScreen" in (call.edits[0].content or "")


def test_rename_refuses_a_missing_source(project):
    call = run_tool(
        project.scanned(),
        {"tool": "rename", "from": "src/screens/Missing.tsx", "to": "src/screens/Other.tsx"},
    )

    assert call.edits == ()
    assert "not found" in call.result
