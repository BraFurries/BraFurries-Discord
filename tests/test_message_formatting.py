import ast
from pathlib import Path
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_function(path: Path, function_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"discord": SimpleNamespace(Attachment=object)}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[function_name]


_replace_member_mentions = _load_function(
    REPOSITORY_ROOT / "core" / "routine_functions.py",
    "_replace_member_mentions",
)
_format_attachments = _load_function(
    REPOSITORY_ROOT / "core" / "discord_events.py",
    "_format_attachments",
)


def test_member_mentions_preserve_display_names_literally():
    display_names = [
        "Coddy User",
        "Nick\\",
        "Nick\\1",
        "Nick\\\\Test",
        "Usuário 🐾 日本語",
    ]

    for index, display_name in enumerate(display_names, start=1):
        member = SimpleNamespace(id=index, display_name=display_name)
        text = f"Olá <@{index}> e <@!{index}>"

        assert _replace_member_mentions(text, [member]) == (
            f"Olá {display_name} e {display_name}"
        )


def test_format_attachments_preserves_empty_behavior():
    assert _format_attachments([]) == ""
    assert _format_attachments(None) == ""


def test_format_attachments_preserves_one_short_url():
    url = "https://cdn.discordapp.com/attachments/1/file.png"

    assert _format_attachments([SimpleNamespace(url=url)]) == url


def test_format_attachments_preserves_multiple_urls_that_fit():
    urls = [f"https://cdn.discordapp.com/attachments/{index}/file.png" for index in range(4)]

    assert _format_attachments([SimpleNamespace(url=url) for url in urls]) == "\n".join(urls)


def test_format_attachments_limits_many_urls_and_reports_omissions():
    attachments = [
        SimpleNamespace(url=f"https://cdn.discordapp.com/attachments/{index}/{'x' * 90}.png")
        for index in range(20)
    ]

    result = _format_attachments(attachments)

    assert len(result) <= 1024
    assert "anexos não exibidos" in result


def test_format_attachments_handles_individually_oversized_urls():
    attachments = [SimpleNamespace(url=f"https://example.com/{'x' * 2048}")]

    result = _format_attachments(attachments)

    assert len(result) <= 1024
    assert result == "… e mais 1 anexo não exibido"
